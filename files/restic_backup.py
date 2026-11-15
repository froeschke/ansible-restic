#!/usr/bin/env python3
"""Restic backup orchestrator managed by Ansible.

The script intentionally uses subprocess argument arrays wherever possible so
paths and user-supplied options are not re-parsed by a shell. Root-managed hook
commands are the one deliberate exception.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import signal
import shlex
import shutil
import subprocess
import sys
import tempfile
from typing import Any

CONFIG = Path("/etc/restic-backup/config.json")


def log(message: str, repository: str | None = None) -> None:
    prefix = f"[restic-backup][{repository}]" if repository else "[restic-backup]"
    print(f"{prefix} {message}", flush=True)


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def log_command(cmd: list[str]) -> None:
    log("exec: " + " ".join(shlex.quote(x) for x in cmd if not _looks_secret(x)))


def run(cmd: list[str], *, env: dict[str, str] | None = None,
        stdout=None, stdin=None, check: bool = True) -> subprocess.CompletedProcess:
    log_command(cmd)
    return subprocess.run(cmd, env=env, stdout=stdout, stdin=stdin, check=check, text=False)


def capture_json(cmd: list[str]) -> Any:
    proc = subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return json.loads(proc.stdout.decode("utf-8"))


def _looks_secret(value: str) -> bool:
    # We do not normally put secrets in argv, but redact obvious URI credentials
    # and password-looking arguments from logs as a second line of defense.
    lower = value.lower()
    return "password=" in lower or ("://" in value and "@" in value)


def safe_name(value: str) -> str:
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_."
    result = "".join(ch if ch in allowed else "_" for ch in value)
    return result or "backup"


def add_source(path: str, sources: list[str], policy: str) -> None:
    p = Path(path)
    if p.exists():
        real = str(p)
        if real not in sources:
            sources.append(real)
    elif policy == "warn":
        log(f"WARNING: backup source does not exist: {path}")
    else:
        raise RuntimeError(f"backup source does not exist: {path}")


def write_env_file(values: dict[str, Any]) -> str:
    for key, value in values.items():
        if "\n" in str(value) or "\r" in str(value):
            raise ValueError(f"environment value for {key} contains a newline")

    fd, path = tempfile.mkstemp(prefix="restic-db-env-", text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            for key, value in values.items():
                handle.write(f"{key}={value}\n")
    except BaseException:
        Path(path).unlink(missing_ok=True)
        raise
    return path


def db_base_command(db: dict[str, Any]) -> tuple[list[str], str]:
    engine = db["engine"]
    database = str(db.get("database", "all"))
    user = db.get("username")
    host = db.get("host")
    port = db.get("port")
    extra = [str(x) for x in db.get("extra_args", [])]

    if engine == "postgresql":
        if database == "all":
            cmd = ["pg_dumpall"]
        else:
            cmd = ["pg_dump", "--format=plain", "--dbname", database]
        if user:
            cmd += ["--username", str(user)]
        if host:
            cmd += ["--host", str(host)]
        if port:
            cmd += ["--port", str(port)]
        return cmd + extra, ".sql"

    if engine in {"mysql", "mariadb"}:
        dump_binary = "mariadb-dump" if engine == "mariadb" else "mysqldump"
        cmd = [dump_binary, "--single-transaction", "--quick", "--routines", "--events", "--triggers"]
        if user:
            cmd += ["--user", str(user)]
        if host:
            cmd += ["--host", str(host)]
        if port:
            cmd += ["--port", str(port)]
        cmd += ["--all-databases"] if database == "all" else ["--databases", database]
        return cmd + extra, ".sql"

    if engine == "mongodb":
        cmd = ["mongodump", "--archive"]
        if database != "all":
            cmd += ["--db", database]
        if user:
            cmd += ["--username", str(user)]
        if host:
            cmd += ["--host", str(host)]
        if port:
            cmd += ["--port", str(port)]
        return cmd + extra, ".archive"

    if engine == "custom":
        command = db.get("command")
        if isinstance(command, str):
            return ["/bin/bash", "-lc", command], str(db.get("extension", ".dump"))
        if isinstance(command, list):
            return [str(x) for x in command], str(db.get("extension", ".dump"))
        raise ValueError(f"custom database {db['name']} requires command string or list")

    raise ValueError(f"unsupported database engine: {engine}")


def dump_database(db: dict[str, Any], output_dir: Path) -> None:
    name = safe_name(str(db["name"]))
    engine = db["engine"]
    command, extension = db_base_command(db)
    output = output_dir / f"{name}{extension}"
    output.parent.mkdir(parents=True, exist_ok=True)
    container = db.get("container")
    environment = {str(k): str(v) for k, v in db.get("environment", {}).items()}
    env_file = None
    mongo_config_path = None

    try:
        if engine == "mongodb" and db.get("mongo_config"):
            # JSON is valid YAML 1.2 and accepted by MongoDB Database Tools as
            # a --config file. This keeps URI/password out of host argv.
            fd, mongo_config_path = tempfile.mkstemp(prefix="restic-mongo-", suffix=".json")
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(db["mongo_config"], handle)

        with output.open("wb") as out:
            if container:
                docker_cmd = ["docker", "exec"]
                if engine == "mongodb" and mongo_config_path:
                    docker_cmd.append("-i")
                if environment:
                    env_file = write_env_file(environment)
                    docker_cmd += ["--env-file", env_file]

                if engine == "mongodb" and mongo_config_path:
                    # Transfer the protected config through stdin into a short-
                    # lived file inside the container; no password is put in argv.
                    shell = (
                        'set -eu; umask 077; cfg=$(mktemp /tmp/restic-mongodump.XXXXXXXX); '
                        'trap \'rm -f "$cfg"\' EXIT; cat > "$cfg"; '
                        'mongodump --config "$cfg" "$@"'
                    )
                    mongo_args = command[1:]  # omit mongodump itself
                    docker_cmd += [str(container), "sh", "-c", shell, "sh", *mongo_args]
                    with open(mongo_config_path, "rb") as cfg_in:
                        run(docker_cmd, stdin=cfg_in, stdout=out)
                else:
                    docker_cmd += [str(container), *command]
                    run(docker_cmd, stdout=out)
            else:
                local_env = os.environ.copy()
                local_env.update(environment)
                if engine == "mongodb" and mongo_config_path:
                    command = [command[0], "--config", mongo_config_path, *command[1:]]
                run(command, env=local_env, stdout=out)
        log(f"database dump complete: {db['name']} -> {output}")
    finally:
        if env_file:
            Path(env_file).unlink(missing_ok=True)
        if mongo_config_path:
            Path(mongo_config_path).unlink(missing_ok=True)


def collect_container(container: dict[str, Any], staging: Path,
                      sources: list[str], policy: str,
                      recovery: list[tuple[str, str]]) -> None:
    name = str(container["name"])
    inspected = capture_json(["docker", "inspect", name])
    if not inspected:
        raise RuntimeError(f"container not found: {name}")
    data = inspected[0]

    if container.get("include_metadata", True):
        meta_dir = staging / "docker" / "containers"
        meta_dir.mkdir(parents=True, exist_ok=True)
        (meta_dir / f"{safe_name(name)}.json").write_text(
            json.dumps(inspected, indent=2), encoding="utf-8")

    for mount in data.get("Mounts", []):
        mtype = mount.get("Type")
        include = (
            (mtype == "volume" and container.get("include_named_volumes", True)) or
            (mtype == "bind" and container.get("include_bind_mounts", True))
        )
        if include and mount.get("Source"):
            add_source(mount["Source"], sources, policy)

    state = data.get("State", {})
    running = bool(state.get("Running"))
    paused = bool(state.get("Paused"))
    quiesce = container.get("quiesce", "none")

    if quiesce == "pause" and running and not paused:
        log(f"pausing container {name}")
        run(["docker", "pause", name])
        recovery.append(("unpause", name))
        log(f"container {name} paused")
    elif quiesce == "stop" and running:
        timeout = str(int(container.get("stop_timeout", 30)))
        run(["docker", "stop", "--time", timeout, name])
        recovery.append(("start", name))


def collect_named_volume(name: str, sources: list[str], policy: str) -> None:
    inspected = capture_json(["docker", "volume", "inspect", name])
    if not inspected or not inspected[0].get("Mountpoint"):
        raise RuntimeError(f"Docker volume has no local mountpoint: {name}")
    add_source(inspected[0]["Mountpoint"], sources, policy)


def recover_containers(recovery: list[tuple[str, str]]) -> list[str]:
    failed = []
    for action, name in reversed(recovery):
        try:
            if action == "unpause":
                log(f"resuming container {name}")
            run(["docker", action, name])
            if action == "unpause":
                log(f"container {name} resumed")
        except Exception as exc:
            failed.append(name)
            log(f"ERROR recovering container {name}: {exc}")
    return failed


def run_hooks(commands: list[str], phase: str) -> None:
    for command in commands:
        log(f"running {phase} hook")
        subprocess.run(["/bin/bash", "-lc", command], check=True)


def _termination_handler(signum, _frame) -> None:
    # Raising SystemExit allows Python finally blocks to run. This is important
    # for always-run hooks such as restarting Docker after `systemctl stop`.
    log(f"received signal {signum}; cleaning up")
    raise SystemExit(128 + signum)


def repository_definitions(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    repos = cfg.get("repositories", [])
    if not repos:
        raise RuntimeError("no restic repositories are configured")
    return repos


def repository_by_name(cfg: dict[str, Any], name: str) -> dict[str, Any]:
    for repo in repository_definitions(cfg):
        if repo.get("name") == name:
            return repo
    raise RuntimeError(f"unknown restic repository: {name}")


def repository_command(cfg: dict[str, Any], name: str, *args: str) -> list[str]:
    return [cfg["wrapper"], "--repository", name, *args]


def snapshot_host(cfg: dict[str, Any]) -> str:
    host = cfg.get("backup_host", os.uname().nodename)
    if not isinstance(host, str) or not host.strip():
        raise ValueError("backup_host must be a non-empty hostname")
    return host


def backup_status(percent: float, bytes_done: int, total_bytes: int,
                  files_done: int, total_files: int, eta: int = 0) -> str:
    message = (
        f"{percent:.1%} | {bytes_done / 2**30:.1f} GiB / {total_bytes / 2**30:.1f} GiB"
        f" | {files_done:,} / {total_files:,} files"
    )
    if eta:
        message += f" | ETA {(eta + 59) // 60}m" if eta >= 60 else f" | ETA {eta}s"
    return message


def run_backup(cfg: dict[str, Any], name: str, cmd: list[str]) -> None:
    if not cfg.get("progress", True):
        run(cmd)
        return

    env = os.environ.copy()
    env["RESTIC_PROGRESS_FPS"] = str(1 / cfg.get("progress_interval", 30))
    cmd = [*cmd[:4], "--json", *cmd[4:]]
    log("starting backup", name)
    log_command(cmd)
    summary = None
    with subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, text=True,
                          encoding="utf-8", errors="replace") as proc:
        try:
            for line in proc.stdout:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    log(line.rstrip(), name)
                    continue
                if event.get("message_type") == "status":
                    total_bytes = event.get("total_bytes", 0)
                    total_files = event.get("total_files", 0)
                    message = "scanning files..."
                    if total_bytes or total_files:
                        message = backup_status(
                            event.get("percent_done", 0),
                            event.get("bytes_done", 0), total_bytes,
                            event.get("files_done", 0), total_files,
                            event.get("seconds_remaining", 0),
                        )
                    log(message, name)
                elif event.get("message_type") == "summary":
                    summary = event
            returncode = proc.wait()
        except BaseException:
            proc.kill()
            raise
    if returncode:
        raise subprocess.CalledProcessError(returncode, cmd)
    if summary is not None:
        total_bytes = summary.get("total_bytes_processed", 0)
        total_files = summary.get("total_files_processed", 0)
        log(backup_status(1, total_bytes, total_bytes, total_files, total_files), name)
        snapshot = summary.get("snapshot_id")
        if snapshot:
            log(f"snapshot completed: {snapshot[:8]}", name)
        else:
            log("backup completed without creating a snapshot", name)


def run_repository_operations(operations: list[tuple[str, list[str]]],
                              backup_config: dict[str, Any] | None = None) -> None:
    errors: list[str] = []
    for name, cmd in operations:
        try:
            log(f"repository {name}")
            if backup_config is not None:
                run_backup(backup_config, name, cmd)
            else:
                run(cmd)
        except Exception as exc:
            errors.append(f"{name}: {exc}")
            log(f"ERROR for repository {name}: {exc}")
    if errors:
        raise RuntimeError("one or more repository operations failed: " + "; ".join(errors))


def restic_backup(cfg: dict[str, Any]) -> None:
    state_dir = Path(cfg["state_dir"])
    staging = state_dir / "staging" / "current"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True, mode=0o700)

    sources: list[str] = []
    recovery: list[tuple[str, str]] = []
    policy = cfg.get("missing_source_policy", "fail")
    succeeded = False

    try:
        # Runs before database dumps and Docker/source discovery.
        run_hooks(cfg.get("pre_backup_commands", []), "pre-backup")

        for db in cfg.get("databases", []):
            dump_database(db, staging / "databases")

        for path in cfg.get("paths", []):
            add_source(str(path), sources, policy)

        for volume in cfg.get("docker_volumes", []):
            collect_named_volume(str(volume), sources, policy)

        for container in cfg.get("containers", []):
            collect_container(container, staging, sources, policy, recovery)

        # Include staging only if it actually contains dumps or metadata.
        if any(staging.iterdir()):
            sources.append(str(staging))

        if not sources:
            raise RuntimeError("no backup sources were resolved")

        # At this point Docker volumes/bind mounts have already been resolved.
        # This phase can therefore safely stop the Docker daemon itself if the
        # user wants all container-backed files fully quiesced.
        run_hooks(cfg.get("pre_snapshot_commands", []), "pre-snapshot")

        with tempfile.NamedTemporaryFile("w", delete=False, prefix="restic-sources-") as src:
            source_file = src.name
            os.chmod(source_file, 0o600)
            for source in sources:
                src.write(source + "\n")

        exclude_file = None
        try:
            backup_args = ["backup", "--files-from", source_file, "--host", snapshot_host(cfg)]
            for tag in cfg.get("backup_tags", []):
                backup_args += ["--tag", str(tag)]
            if cfg.get("one_file_system"):
                backup_args.append("--one-file-system")
            if cfg.get("excludes"):
                with tempfile.NamedTemporaryFile("w", delete=False, prefix="restic-excludes-") as exc:
                    exclude_file = exc.name
                    os.chmod(exclude_file, 0o600)
                    for pattern in cfg["excludes"]:
                        exc.write(str(pattern) + "\n")
                backup_args += ["--exclude-file", exclude_file]
            backup_args += [str(x) for x in cfg.get("backup_extra_args", [])]

            repos = repository_definitions(cfg)
            mode = cfg.get("repository_mode", "direct")
            primary_name = cfg.get("primary_repository") or repos[0]["name"]

            if mode == "direct":
                operations = [
                    (repo["name"], repository_command(cfg, repo["name"], *backup_args))
                    for repo in repos
                ]
                # Try every repository even if an earlier provider is down. The
                # job is considered failed if any repository failed.
                run_repository_operations(operations, backup_config=cfg)
            elif mode == "copy":
                primary = repository_by_name(cfg, primary_name)
                run_backup(cfg, primary_name, repository_command(cfg, primary_name, *backup_args))

                copy_operations: list[tuple[str, list[str]]] = []
                for repo in repos:
                    if repo["name"] == primary_name:
                        continue
                    copy_args = [
                        "copy",
                        "--from-repo", str(primary["repository"]),
                        "--from-password-file", str(primary["password_file"]),
                        "--host", snapshot_host(cfg),
                        *[str(x) for x in cfg.get("copy_extra_args", [])],
                    ]
                    copy_operations.append(
                        (repo["name"], repository_command(cfg, repo["name"], *copy_args))
                    )
                run_repository_operations(copy_operations)
            else:
                raise RuntimeError(f"unsupported repository mode: {mode}")

            succeeded = True
        finally:
            Path(source_file).unlink(missing_ok=True)
            if exclude_file:
                Path(exclude_file).unlink(missing_ok=True)

        # Success-only hooks are appropriate for notifications/metrics that
        # should only be emitted once the snapshot has been committed.
        run_hooks(cfg.get("post_backup_commands", []), "post-backup")
    except BaseException:
        # Failure hooks are best-effort and must never hide the original error
        # (including SIGTERM/SIGINT converted to SystemExit by our handler).
        try:
            run_hooks(cfg.get("failed_backup_commands", []), "failed-backup")
        except Exception as hook_exc:
            log(f"ERROR in failed-backup hook: {hook_exc}")
        raise
    finally:
        # `finally` hooks run before Docker container recovery. This lets a hook
        # such as `systemctl start docker` make the Docker API available again
        # before paused/stopped containers are recovered below.
        active_exception = sys.exc_info()[0] is not None
        finally_error: Exception | None = None
        try:
            run_hooks(cfg.get("finish_backup_commands", []), "finish-backup")
        except Exception as hook_exc:
            finally_error = hook_exc
            log(f"ERROR in finish-backup hook: {hook_exc}")
        finally:
            failed_recoveries = recover_containers(recovery)
            if (succeeded and not active_exception and finally_error is None
                    and not failed_recoveries and cfg.get("cleanup_staging", True)):
                shutil.rmtree(staging, ignore_errors=True)

        # If the backup itself succeeded, a failed mandatory cleanup hook should
        # make the systemd unit fail visibly. If an earlier phase already failed,
        # preserve that original exception instead of masking it.
        if finally_error is not None and not active_exception:
            raise finally_error
        if failed_recoveries and not active_exception:
            raise RuntimeError("failed to recover containers: " + ", ".join(failed_recoveries))


def restic_forget(cfg: dict[str, Any]) -> None:
    retention = cfg.get("retention", {})
    forget_args = ["forget", "--prune", "--host", snapshot_host(cfg)]
    mapping = {
        "keep_last": "--keep-last",
        "keep_hourly": "--keep-hourly",
        "keep_daily": "--keep-daily",
        "keep_weekly": "--keep-weekly",
        "keep_monthly": "--keep-monthly",
        "keep_yearly": "--keep-yearly",
        "keep_within": "--keep-within",
        "group_by": "--group-by",
    }
    for key, flag in mapping.items():
        value = retention.get(key)
        if value is not None and value != "":
            forget_args += [flag, str(value)]
    run_repository_operations([
        (repo["name"], repository_command(cfg, repo["name"], *forget_args))
        for repo in repository_definitions(cfg)
    ])


def restic_check(cfg: dict[str, Any]) -> None:
    check_args = ["check", *[str(x) for x in cfg.get("check_extra_args", [])]]
    run_repository_operations([
        (repo["name"], repository_command(cfg, repo["name"], *check_args))
        for repo in repository_definitions(cfg)
    ])


def with_lock(cfg: dict[str, Any], action) -> None:
    lock_path = Path(cfg.get("lock_file", "/run/lock/restic-backup.lock"))
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("another restic backup/maintenance operation is already running")
        action(cfg)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=CONFIG)
    parser.add_argument("action", choices=["backup", "forget", "check"])
    args = parser.parse_args()
    cfg = load_config(args.config)
    signal.signal(signal.SIGTERM, _termination_handler)
    signal.signal(signal.SIGINT, _termination_handler)
    actions = {"backup": restic_backup, "forget": restic_forget, "check": restic_check}
    try:
        with_lock(cfg, actions[args.action])
    except Exception as exc:
        log(f"ERROR: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

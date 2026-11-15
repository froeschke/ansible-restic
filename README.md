# Ansible role: `restic_backup`

A self-contained Ansible role for encrypted Restic backups to one or more
Amazon S3, S3-compatible, or other Restic-supported repositories. It supports normal files/directories, Docker named
volumes, Docker container bind/volume mounts and metadata, and logical database
dumps for PostgreSQL, MySQL/MariaDB and MongoDB.

## Why the role treats databases separately

Copying a live database data directory is not a reliable logical backup. This
role dumps databases first, then includes those dump files in the same Restic
snapshot. For non-database container data, a container may optionally be
paused or stopped while Restic reads its mounts.

Restic repositories are encrypted by design. This role stores the repository
password and S3 credentials in root-only files on the managed host. Keep the
source values in Ansible Vault or another Ansible secret source. When a source
includes a parent of `restic_config_dir`, exclude that directory so backups
omit the role's repository credentials and database configuration. Keep local
repositories and the Restic cache outside the source paths.

## Requirements

- Linux with systemd, gathered Ansible facts, and privilege escalation to root.
  The role metadata lists Debian 12 (Bookworm) and Debian 13 (Trixie).
- `ansible-core` 2.15 or newer with the Python versions required by your chosen
  release on the controller and managed host. The backup script itself requires
  Python 3.8 or newer. See the [Ansible Python requirements](https://docs.ansible.com/ansible/latest/installation_guide/intro_installation.html#node-requirement-summary).
- Restic 0.14 or newer from the target's package manager. Set
  `restic_package_name` if your source uses a different package name. The `fastest`
  and `better` compression modes require [Restic 0.19 or newer](https://restic.net/blog/2026-06-09/restic-0.19.0-released/).
- Docker CLI when Docker features are used
- For a database running inside a container, its dump client must exist in the
  container (`pg_dump`, `mysqldump`, `mariadb-dump`, or `mongodump`). For host databases, the
  corresponding client must exist on the host. Add packages through
  `restic_extra_packages` if desired.

## Quick start

From your Ansible project directory, clone the role into `roles/restic_backup`.

```bash
git clone https://github.com/froeschke/ansible-restic.git roles/restic_backup
```

If your project uses a `requirements.yml` file, you can install from Git by
adding this role entry. See the [Ansible role installation guide](https://docs.ansible.com/projects/ansible/latest/galaxy/user_guide.html#installing-multiple-roles-from-a-file).

```yaml
roles:
  - name: restic_backup
    src: https://github.com/froeschke/ansible-restic.git
    scm: git
```

If you authenticate to GitHub over SSH, use this `src` value with the same role entry.

```yaml
src: git+ssh://git@github.com/froeschke/ansible-restic.git
```

```bash
ansible-galaxy role install -r requirements.yml --roles-path roles
```

Add a Linux host to the `backup_hosts` inventory group and store its repository
password as `vault_restic_password` in Ansible Vault. Run the included example
from the project directory, substituting the path to your inventory:

```bash
ANSIBLE_ROLES_PATH="$PWD/roles" ansible-playbook -i inventory.yml \
  roles/restic_backup/examples/playbook.yml --ask-vault-pass
```

The example backs up `/etc/hosts` to `/srv/restic-repository` on the managed
host and leaves the retention timer disabled. To take and verify the first
snapshot on that host:

```bash
sudo systemctl start restic-backup.service
sudo /usr/local/sbin/restic-run snapshots
```

Choose an off-host repository for backups intended to survive host or disk loss.
The complete variable list and defaults are in [defaults/main.yml](defaults/main.yml).
See [examples/host_vars.yml](examples/host_vars.yml) for a multi-repository setup.

## S3 / S3-compatible configuration

You can supply the complete Restic repository string:

```yaml
restic_repository: "s3:https://s3.example.net/backups/server01"
```

or compose it:

```yaml
restic_s3_endpoint: "https://s3.example.net"
restic_s3_bucket: backups
restic_s3_prefix: "{{ inventory_hostname }}"
restic_s3_bucket_lookup: auto  # auto, path, dns
restic_s3_region: eu-central-1
```

For AWS itself, an endpoint such as `s3.eu-central-1.amazonaws.com` can be used.
The role passes `s3.bucket-lookup` and, when set, `s3.region` to Restic. Extra
backend settings can be added with `restic_s3_options`:

```yaml
restic_s3_options:
  storage-class: STANDARD_IA
```


## Multiple repositories

The role can write the same backup job to multiple independent Restic
repositories. Existing single-repository variables remain supported; use
`restic_repositories` when you need more than one destination.

For AWS S3 plus Backblaze B2 with separate credentials, use `direct` mode:

```yaml
restic_password: "{{ vault_restic_password }}"
restic_repository_mode: direct
restic_primary_repository: backblaze

restic_repositories:
  - name: backblaze
    s3_endpoint: "https://s3.eu-central-003.backblazeb2.com"
    s3_bucket: server-backups
    s3_prefix: "{{ inventory_hostname }}"
    s3_bucket_lookup: path
    s3_access_key: "{{ vault_backblaze_key_id }}"
    s3_secret_key: "{{ vault_backblaze_application_key }}"

  - name: aws
    s3_endpoint: "https://s3.eu-central-1.amazonaws.com"
    s3_bucket: server-backups
    s3_prefix: "{{ inventory_hostname }}"
    s3_region: eu-central-1
    s3_access_key: "{{ vault_aws_access_key }}"
    s3_secret_key: "{{ vault_aws_secret_key }}"
```

`direct` mode creates an independent snapshot in every repository. Database
dumps, Docker discovery and lifecycle hooks run only once, but Restic reads the
resolved source paths once per repository. If one provider fails, the role still
attempts the remaining repositories and then marks the job failed if any
destination failed.

### Copy mode

To back up only to a primary repository and replicate its snapshots with
Restic's `copy` command:

```yaml
restic_repository_mode: copy
restic_primary_repository: primary

restic_repositories:
  - name: primary
    repository: "rclone:primary:restic/{{ inventory_hostname }}"

  - name: secondary
    repository: "rclone:secondary:restic/{{ inventory_hostname }}"
```

In `copy` mode the role:

1. creates the new snapshot in the primary repository;
2. copies snapshots for `restic_backup_host` that are missing from each
   secondary repository;
3. initializes new secondary repositories with `--copy-chunker-params` so they
   use the primary repository's chunker parameters;
4. runs retention/prune and repository checks independently on every
   repository.

Additional arguments for `restic copy` can be supplied with:

```yaml
restic_copy_extra_args:
  - --verbose
```

**Native S3 credential limitation:** Restic opens both repositories in the same
process during `copy`. Backend environment/options can therefore apply to both
repositories. The role rejects copy mode when two native `s3:` repositories
use different S3 credentials. AWS S3 + Backblaze B2 with separate application
keys should use `direct` mode. If copy mode is important across providers, use
independent `rclone:` remotes so rclone owns each provider's credentials. Add
`rclone` to `restic_extra_packages` (or install/configure it separately) when
using `rclone:` repository URLs.

Each repository may override `password` and `compression`; otherwise the global
`restic_password` and `restic_compression` values are inherited. Reusing the
same Restic password is convenient but not required.

## Secrets with Ansible Vault

Example `group_vars/all/vault.yml`:

```yaml
vault_restic_password: "use-a-long-random-value"
vault_backup_access_key: "..."
vault_backup_secret_key: "..."
vault_postgres_password: "..."
```

Encrypt it:

```bash
ansible-vault encrypt group_vars/all/vault.yml
```

Reference those values from normal vars:

```yaml
restic_password: "{{ vault_restic_password }}"
restic_s3_access_key: "{{ vault_backup_access_key }}"
restic_s3_secret_key: "{{ vault_backup_secret_key }}"
```

Do not commit plaintext secrets.

## Complete example

```yaml
---
- name: Configure backups
  hosts: docker_hosts
  become: true
  roles:
    - role: restic_backup
      vars:
        restic_s3_endpoint: "https://s3.example.net"
        restic_s3_bucket: infrastructure-backups
        restic_s3_prefix: "{{ inventory_hostname }}"
        restic_s3_region: eu-central-1
        restic_s3_bucket_lookup: path
        restic_s3_access_key: "{{ vault_backup_access_key }}"
        restic_s3_secret_key: "{{ vault_backup_secret_key }}"
        restic_password: "{{ vault_restic_password }}"

        # Restic repository format v2 supports compression.
        restic_compression: auto

        # Files and folders are both supported.
        restic_paths:
          - /etc
          - /opt/docker/compose.yml
          - /srv/app/uploads

        restic_excludes:
          - "{{ restic_config_dir }}"
          - "**/.cache/**"
          - "**/node_modules/**"

        # Back up named volumes even if they are not listed through a container.
        restic_docker_volumes:
          - traefik_certs

        # Back up Docker metadata and local mount sources. `pause` is useful for
        # ordinary app files; use logical dumps below for databases.
        restic_containers:
          - name: app
            quiesce: pause
            include_metadata: true
            include_named_volumes: true
            include_bind_mounts: true

          - name: redis
            quiesce: stop
            stop_timeout: 20
            include_metadata: true
            include_named_volumes: true
            include_bind_mounts: false

          # Database container metadata can be captured without raw volume copy.
          - name: postgres
            quiesce: none
            include_metadata: true
            include_named_volumes: false
            include_bind_mounts: false

        restic_databases:
          - name: app-postgres
            engine: postgresql
            container: postgres
            database: app
            username: postgres
            environment:
              PGPASSWORD: "{{ vault_postgres_password }}"

          - name: legacy-mariadb
            engine: mariadb
            container: mariadb
            database: all
            username: root
            environment:
              MYSQL_PWD: "{{ vault_mysql_root_password }}"

          - name: documents-mongo
            engine: mongodb
            container: mongodb
            database: documents
            mongo_config:
              uri: "{{ vault_mongodb_uri }}"

        restic_backup_tags:
          - docker-host
          - production

        restic_backup_on_calendar: "*-*-* 02:00:00"
        restic_retention:
          keep_last: null
          keep_hourly: null
          keep_daily: 7
          keep_weekly: 4
          keep_monthly: 12
          keep_yearly: 3
          keep_within: ""
          group_by: "host,paths"

        # Example: read a rolling 5% of pack data during the weekly check.
        restic_check_extra_args:
          - "--read-data-subset=5%"
```

## Database options

Every database item accepts:

```yaml
- name: unique-name
  engine: postgresql       # postgresql | mysql | mariadb | mongodb | custom
  container: optional-name # omit to run the client on the host
  database: app            # use "all" for all databases
  username: optional-user
  host: optional-host
  port: optional-port
  environment: {}          # protected; useful for PGPASSWORD / MYSQL_PWD
  extra_args: []
```

### PostgreSQL

For one database the role runs a plain `pg_dump`; for `database: all` it runs
`pg_dumpall`. Plain SQL is deliberate: Restic can deduplicate and compress it
more effectively than a pre-compressed dump.

Typical restore:

```bash
psql -U postgres -d app < app-postgres.sql
```

### MySQL / MariaDB

For `engine: mysql`, the role uses `mysqldump`. For `engine: mariadb`, it uses
the MariaDB-native `mariadb-dump` command. This applies to both host databases
and databases running inside containers. Both use:

```text
--single-transaction --quick --routines --events --triggers
```

Typical MariaDB restore:

```bash
mariadb -u root app < legacy-mariadb.sql
```

### MongoDB

Use `mongo_config` for sensitive `mongodump --config` values. MongoDB documents
`--config` as the preferred non-interactive way to provide sensitive values.
For a containerized MongoDB, the role transfers this config over stdin to a
short-lived mode-0600 file inside the container, then removes it.

Typical restore:

```bash
mongorestore --archive=documents-mongo.archive
```

### Custom dump command

Anything that can write its backup to stdout can be included:

```yaml
restic_databases:
  - name: custom-service
    engine: custom
    command:
      - /usr/local/bin/export-service
      - --stdout
    extension: .dump
```

A string command is also supported and is executed with `/bin/bash -lc`. Treat
string commands as trusted root configuration.

## Docker consistency modes

`restic_containers[].quiesce` supports:

- `none`: do not change container state.
- `pause`: freeze a running container before Restic reads its mounts, then
  unpause it after the backup.
- `stop`: stop a running container before Restic reads its mounts, then start
  it afterward. `stop_timeout` defaults to 30 seconds.

Only containers that were actually changed by the backup are recovered. If
Restic fails, the role still attempts to unpause/start them in a `finally`
path.

Docker metadata is stored under
`/var/lib/restic-backup/staging/current/docker/containers/` inside the snapshot.
Logical database dumps are stored under the sibling `databases/` directory.
Staging data is deleted after a successful backup by default and retained after
a failed backup for troubleshooting.

> Docker volumes must expose a local `Mountpoint`. Remote volume drivers and a
> remote Docker Engine may not provide a path Restic on the host can read.

## Compression

`restic_compression` accepts the Restic modes:

```yaml
restic_compression: auto     # default
# off | fastest | better | max are also supported
```

Compression is a repository-format-v2 feature. New repositories are explicitly
initialized as v2 by this role. If you point the role at an old v1 repository,
migrate that repository deliberately before enabling compression rather than
having automation migrate it implicitly.

## Scheduling

The role creates three independent systemd timer/service pairs:

- `restic-backup.timer` - backup; default daily at 02:00
- `restic-forget.timer` - retention + prune; default Sundays at 04:00
- `restic-check.timer` - repository check; default Sundays at 05:30

Scheduled retention applies to snapshots whose hostname matches
`restic_backup_host`, which defaults to `inventory_hostname`. Give each
independent backup job a distinct hostname when jobs share a repository. If
you change the hostname, review retention for snapshots under the old name
separately. See [Restic's retention documentation](https://restic.readthedocs.io/en/stable/060_forget.html).

Useful commands:

```bash
systemctl list-timers 'restic-*'
systemctl start restic-backup.service
journalctl -u restic-backup.service
```

Follow backup progress live with `sudo journalctl -u restic-backup.service -f`.
Each backup logs its destination, processed bytes and files, ETA when
available, and the ID of the snapshot it creates. Container pause and resume
events also appear in the journal.

```yaml
restic_progress: true
restic_progress_interval: 30  # seconds between updates
```

These are the defaults. Set `restic_progress: false` to use native Restic
output. In copy mode, these settings apply to the primary backup. Replication
uses native `restic copy` output.

Maintenance can also be run manually:

```bash
/usr/local/sbin/restic-backup forget
/usr/local/sbin/restic-backup check
```

If you set `restic_config_dir`, pass `--config /your/config/directory/config.json`
when invoking `restic-backup` manually. The systemd services already use that
configuration file.

Use Restic directly through the protected wrapper. Without `--repository`,
the configured primary repository is used:

```bash
/usr/local/sbin/restic-run snapshots
/usr/local/sbin/restic-run ls latest
/usr/local/sbin/restic-run restore latest --target /tmp/restore

# Select a specific configured repository:
/usr/local/sbin/restic-run --repository aws snapshots
/usr/local/sbin/restic-run --repository backblaze check
```

## Restore examples

List snapshots:

```bash
sudo /usr/local/sbin/restic-run snapshots
```

Restore the latest snapshot to a scratch directory first:

```bash
sudo /usr/local/sbin/restic-run restore latest --target /srv/restore-test
```

For Docker volumes, stop the consuming container and copy the restored content
back to the volume only after verifying ownership/permissions. For databases,
restore the logical dump with the database-native restore client instead of
copying live data-directory files.

## Backup lifecycle hooks

The role supports five hook phases. Hook commands run as `root` through
`/bin/bash -lc`, so treat them as trusted configuration.

| Variable | When it runs | Typical use |
| --- | --- | --- |
| `restic_pre_backup_commands` | Before database dumps and source discovery | Flush an application, create an export |
| `restic_pre_snapshot_commands` | After all paths/Docker mounts/database dumps are resolved, immediately before Restic reads them | Stop Docker or another service for a filesystem-consistent snapshot |
| `restic_post_backup_commands` | Only after Restic successfully commits the snapshot | Success notification/metric |
| `restic_failed_backup_commands` | When the backup fails or is interrupted | Failure notification/diagnostics |
| `restic_finish_backup_commands` | Always, on success, failure, SIGINT, or SIGTERM | Restart services that must never stay stopped |

A common whole-Docker-host setup is:

```yaml
restic_pre_snapshot_commands:
  - systemctl stop docker.service docker.socket

restic_finish_backup_commands:
  - systemctl start docker.socket docker.service
```

`pre_snapshot` is deliberately separate from `pre_backup`: Docker container
and volume discovery and logical database dumps happen first. Only then is
Docker stopped, so configured `restic_containers` and `restic_docker_volumes`
can still be resolved. The `finish` phase runs before the role's own
container-recovery logic, which means Docker is available again if the role
needs to restart/unpause containers.

You can combine phases, for example:

```yaml
restic_pre_backup_commands:
  - /usr/local/sbin/app-flush

restic_pre_snapshot_commands:
  - systemctl stop myapp.service

restic_post_backup_commands:
  - /usr/local/sbin/backup-success-metric

restic_failed_backup_commands:
  - /usr/local/sbin/backup-failed-alert

restic_finish_backup_commands:
  - systemctl start myapp.service
```

If a `finish` hook fails after an otherwise successful backup, the
systemd unit is marked failed so the recovery problem is visible. If the backup
had already failed, a failing cleanup hook is logged without masking the
original backup error.

The orchestrator also handles `SIGTERM`/`SIGINT` so `systemctl stop
restic-backup.service` still executes `restic_finish_backup_commands` and the
role's container recovery path before exiting. Avoid `SIGKILL`, because no
process can run cleanup code after a forced kill.

## S3 immutability

Restic protects confidentiality and integrity cryptographically, but credentials
on the backed-up host may still be able to delete repository objects. For
ransomware resistance, combine this role with provider-side versioning,
retention/immutability, or an appropriately restricted credential model where
your S3 provider supports it. Test the exact combination with Restic before
relying on it operationally.

## Testing backups

Ansible check mode previews package and file changes. It skips repository
initialization and the install-time timer state changes.

A backup is not proven until it restores. Periodically run a restore into a
scratch path and test database imports. `restic check` validates repository
structure; `--read-data` or `--read-data-subset` can additionally verify pack
data at the cost of more S3 reads.

### Testing the role from a checkout

Run these commands from the repository root. The symlink lets Ansible find the
role by its declared name regardless of the checkout directory name.

```bash
role_search_path=$(mktemp -d)
ln -s "$PWD" "$role_search_path/restic_backup"
export ANSIBLE_ROLES_PATH="$role_search_path"

ansible-playbook -i tests/inventory tests/test.yml --syntax-check
ansible-lint --offline --profile shared
python3 -m unittest discover -s tests
```

On a disposable Linux VM with systemd and Restic 0.14 or newer available
through the package manager, omit `--syntax-check` to apply the smoke-test
playbook. It installs Restic and writes the role's systemd units and local
configuration on localhost. The two plays exercise the single- and
multi-repository configuration paths with timers and repository initialization
disabled.

## Uninstall / remove the local Restic setup

The role has a safe lifecycle switch. To remove the backup setup from a host:

```yaml
restic_backup_state: absent
```

With `absent`, the role:

- stops and disables `restic-backup.timer`, `restic-forget.timer`, and
  `restic-check.timer` when they exist;
- stops any active role-managed Restic services;
- removes the six role-managed systemd service/timer unit files;
- removes `/usr/local/sbin/restic-backup` and `/usr/local/sbin/restic-run`;
- removes the configured local config, state, and cache directories (by default
  `/etc/restic-backup`, `/var/lib/restic-backup`, and `/var/cache/restic-backup`);
- reloads systemd.

It **never deletes, prunes, empties, or otherwise modifies the remote Restic
repository or S3/B2 bucket data**. Repository credentials and
`restic_password` are not required when `restic_backup_state: absent`.

Use dedicated directories for `restic_config_dir`, `restic_state_dir`, and
`restic_cache_dir`. Uninstall removes their contents recursively.

The Restic OS package is kept by default. To remove it as well:

```yaml
restic_backup_state: absent
restic_remove_package: true
```

`restic_remove_package` only removes `restic_package_name`; packages listed in
`restic_extra_packages` are intentionally left installed because they may be
used by other applications.

For example, an uninstall play can be as small as:

```yaml
---
- name: Remove Restic backup configuration
  hosts: servers
  become: true
  roles:
    - role: restic_backup
      vars:
        restic_backup_state: absent
        restic_remove_package: true
```

Running that play repeatedly is safe and idempotent, and your remote backups
remain available for restoration from another Restic installation.

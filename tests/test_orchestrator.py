import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "files"))
import restic_backup


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "wrapper": "/usr/local/sbin/restic-run",
            "backup_host": "backup-node",
            "repositories": [{"name": "primary"}, {"name": "secondary"}],
            "retention": {"keep_last": 3},
        }

    @patch.object(restic_backup, "run")
    def test_forget_filters_every_repository_by_snapshot_host(self, run):
        restic_backup.restic_forget(self.config)

        self.assertEqual([call.args[0] for call in run.call_args_list], [
            [self.config["wrapper"], "--repository", name, "forget", "--prune",
             "--host", "backup-node", "--keep-last", "3"]
            for name in ("primary", "secondary")
        ])

    @patch.object(restic_backup, "run")
    def test_forget_rejects_empty_host_before_calling_restic(self, run):
        self.config["backup_host"] = " "

        with self.assertRaisesRegex(ValueError, "non-empty hostname"):
            restic_backup.restic_forget(self.config)

        run.assert_not_called()

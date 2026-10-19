"""backup_snapshot.sh: atomic publish, rotation only after success."""
import os
import sqlite3
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / 'backups' / 'backup_snapshot.sh'


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.app, self.conf, self.backups = root / 'app', root / 'conf', root / 'backups'
        (self.app / 'data').mkdir(parents=True)
        (self.app / 'node_modules').mkdir()
        self.conf.mkdir()
        db = sqlite3.connect(self.app / 'data' / 'app.db')
        db.execute('CREATE TABLE items (id INTEGER PRIMARY KEY)')
        db.execute('INSERT INTO items DEFAULT VALUES')
        db.commit()
        db.close()
        (self.app / 'node_modules' / 'big.js').write_text('vendored')
        (self.app / '.env').write_text('TOKEN=not-real\n')
        self.write_conf()

    def write_conf(self, dump_cmd=''):
        (self.conf / 'app-a.env').write_text(
            f'SOURCE_DIR={self.app}\nSQLITE_DBS="data/app.db"\nEXCLUDE="node_modules"\n'
            f'DUMP_CMD="{dump_cmd}"\nKEEP=2\n')

    def run_backup(self, **env):
        e = dict(os.environ, REQUIRE_MOUNT='0', BACKUP_CONF_DIR=str(self.conf), **env)
        if 'KEEP' not in env:
            e.pop('KEEP', None)
        return subprocess.run([str(SCRIPT), 'app-a', str(self.backups)], capture_output=True,
                              text=True, env=e)

    def snapshots(self):
        return sorted(p.name for p in (self.backups / 'app-a').iterdir()
                      if p.is_dir() and not p.is_symlink() and not p.name.startswith('.'))

    def test_snapshot_content_and_latest(self):
        proc = self.run_backup()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        latest = self.backups / 'app-a' / 'latest'
        self.assertTrue(latest.is_symlink())
        self.assertEqual(os.readlink(latest), self.snapshots()[-1])        # relative target
        self.assertTrue((latest / 'data' / 'app.db').is_file())
        self.assertTrue((latest / '.env').is_file())
        self.assertFalse((latest / 'node_modules').exists())
        sums = (latest / 'SHA256SUMS').read_text()
        self.assertIn('./data/app.db', sums)
        self.assertNotIn('SHA256SUMS', sums)
        self.assertEqual((self.backups / 'app-a').stat().st_mode & 0o777, 0o700)

    def test_rotation_keeps_keep_and_cron_keep_wins(self):
        for _ in range(3):
            self.assertEqual(self.run_backup().returncode, 0)
            time.sleep(1.05)
        self.assertEqual(len(self.snapshots()), 2)
        self.assertEqual(self.run_backup(KEEP='1').returncode, 0)
        self.assertEqual(len(self.snapshots()), 1)

    def test_failed_run_keeps_retention_and_latest(self):
        self.assertEqual(self.run_backup().returncode, 0)
        time.sleep(1.05)
        self.assertEqual(self.run_backup().returncode, 0)
        before = (self.snapshots(), os.readlink(self.backups / 'app-a' / 'latest'))
        time.sleep(1.05)
        self.write_conf(dump_cmd='exit 1')
        proc = self.run_backup()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('DUMP_CMD failed', proc.stderr.strip().splitlines()[-1])
        self.assertEqual((self.snapshots(), os.readlink(self.backups / 'app-a' / 'latest')), before)
        self.assertEqual([p for p in (self.backups / 'app-a').iterdir() if p.name.startswith('.partial')], [])

    def test_refuses_an_unmounted_backup_root(self):
        e = dict(os.environ, BACKUP_CONF_DIR=str(self.conf))
        e.pop('REQUIRE_MOUNT', None)
        proc = subprocess.run([str(SCRIPT), 'app-a', str(self.backups)], capture_output=True, text=True, env=e)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('not mounted', proc.stderr)


if __name__ == '__main__':
    unittest.main()

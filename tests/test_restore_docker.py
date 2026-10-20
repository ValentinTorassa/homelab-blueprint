"""The mailer family against a real Postgres, in a throwaway container.

Needs docker and the image in RT_POSTGRES_IMAGE (default postgres:16-alpine)
already on the host, because the restore test never pulls. Without them these
tests are skipped, unless RT_REQUIRE_DOCKER=1 (set in CI): then a missing
prerequisite is an error, so the job cannot go green without having restored
anything.

    docker pull postgres:16-alpine && make test-docker
"""
import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backups'))
import restore_test as rt  # noqa: E402

IMAGE = os.environ.get('RT_POSTGRES_IMAGE', 'postgres:16-alpine')
REQUIRED = os.environ.get('RT_REQUIRE_DOCKER') == '1'
SNAP = '2026-01-05_02-30-00'

DUMP = """\
CREATE TABLE subscribers (id serial PRIMARY KEY, email text NOT NULL UNIQUE, status text NOT NULL);
INSERT INTO subscribers (email, status) VALUES ('a@example.com', 'enabled');
INSERT INTO subscribers (email, status) VALUES ('b@example.com', 'enabled');
INSERT INTO subscribers (email, status) VALUES ('c@example.com', 'blocklisted');
"""


def missing_prerequisite():
    try:
        info = subprocess.run(['docker', 'info'], capture_output=True, stdin=subprocess.DEVNULL, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return 'docker is not installed'
    if info.returncode != 0:
        return 'the docker daemon is not reachable'
    image = subprocess.run(['docker', 'image', 'inspect', IMAGE], capture_output=True, stdin=subprocess.DEVNULL)
    if image.returncode != 0:
        return f'{IMAGE} is not on the host (docker pull {IMAGE})'
    return None


def leftovers(run_id):
    out = subprocess.run(['docker', 'ps', '-a', '--filter', f'label={rt.LABEL}.run={run_id}',
                          '--format', '{{.Names}}'], capture_output=True, text=True).stdout
    return out.split()


class MailerAgainstPostgres(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        missing = missing_prerequisite()
        if missing and REQUIRED:
            raise RuntimeError(f'RT_REQUIRE_DOCKER=1 but {missing}')
        if missing:
            raise unittest.SkipTest(missing)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.backups = Path(self.tmp.name) / 'backups'
        self.scratch = Path(self.tmp.name) / 'scratch'
        self.scratch.mkdir()
        self.run_id = f'it-{os.getpid()}-{self._testMethodName[5:20]}'.replace('_', '-')

    def snapshot(self, dump: str):
        """A mailer snapshot as backup_snapshot.sh writes it: dump.sql plus SHA256SUMS.
        The checksum covers the dump as written, so a bad dump passes it and only
        loading it can tell."""
        d = self.backups / 'mailer' / SNAP
        d.mkdir(parents=True)
        (d / 'dump.sql').write_text(dump)
        digest = hashlib.sha256(dump.encode()).hexdigest()
        (d / 'SHA256SUMS').write_text(f'{digest}  ./dump.sql\n')
        (self.backups / 'mailer' / 'latest').symlink_to(SNAP)

    def restore(self):
        result = rt.run_one('mailer', self.backups, self.scratch, self.run_id, min_free_gb=0)
        self.assertEqual(leftovers(self.run_id), [], 'a throwaway container was left behind')
        return result

    def test_a_good_dump_loads_and_has_rows(self):
        self.snapshot(DUMP)
        result = self.restore()
        self.assertEqual(result['status'], rt.PASS, result['detail'])
        self.assertEqual(result['facts']['subscribers'], 3)

    def test_a_dump_cut_off_mid_statement_fails(self):
        # The dump job was killed halfway through writing: SHA256SUMS matches the
        # truncated file, and the snapshot looks complete from the outside.
        self.snapshot(DUMP + "INSERT INTO subscribers (email, status) VALUES ('d@example.com',")
        result = self.restore()
        self.assertEqual(result['status'], rt.FAIL)
        self.assertIn('dump did not load', result['detail'])

    def test_a_dump_with_no_rows_fails(self):
        self.snapshot(DUMP.splitlines()[0] + '\n')
        result = self.restore()
        self.assertEqual(result['status'], rt.FAIL)
        self.assertIn('subscribers is empty', result['detail'])


if __name__ == '__main__':
    unittest.main()

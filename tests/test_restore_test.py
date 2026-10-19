import hashlib
import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backups'))
import restore_test as rt  # noqa: E402

SNAP = '2026-01-05_02-30-00'


def make_app_a(backup_root: Path, snap=SNAP, attachments=3, with_env=True):
    """A snapshot shaped like the ones backup_snapshot.sh writes for app-a."""
    d = backup_root / 'app-a' / snap
    (d / 'data').mkdir(parents=True)
    (d / 'uploads').mkdir()
    db = sqlite3.connect(d / 'data' / 'app.db')
    db.executescript('CREATE TABLE items (id INTEGER PRIMARY KEY, title TEXT);'
                     'CREATE TABLE attachments (id INTEGER PRIMARY KEY, item_id INT, ext TEXT, size_bytes INT);')
    for i in range(1, attachments + 1):
        data = b'x' * (100 * i)
        (d / 'uploads' / f'{i}.png').write_bytes(data)
        db.execute('INSERT INTO items (title) VALUES (?)', (f'item {i}',))
        db.execute('INSERT INTO attachments (item_id, ext, size_bytes) VALUES (?, ?, ?)', (i, 'png', len(data)))
    db.commit()
    db.close()
    if with_env:
        (d / '.env').write_text('API_TOKEN=not-real\n')
    lines = []
    for p in sorted(x for x in d.rglob('*') if x.is_file()):
        lines.append(f'{hashlib.sha256(p.read_bytes()).hexdigest()}  ./{p.relative_to(d)}')
    (d / 'SHA256SUMS').write_text('\n'.join(lines) + '\n')
    link = backup_root / 'app-a' / 'latest'
    if link.is_symlink():
        link.unlink()
    link.symlink_to(snap)
    return d


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.backups = self.root / 'backups'
        self.scratch = self.root / 'scratch'
        self.scratch.mkdir()

    def ctx(self, family='app-a', **caps):
        caps.setdefault('min_free_gb', 0)
        return rt.Context(family, self.backups, self.scratch, 'test', **caps)


class FamilyTests(Base):
    def test_good_snapshot_passes_and_secrets_are_not_copied(self):
        make_app_a(self.backups)
        result = rt.run_one('app-a', self.backups, self.scratch, 'test', min_free_gb=0)
        self.assertEqual(result['status'], rt.PASS, result['detail'])
        self.assertEqual(result['facts']['attachments.ok'], 3)
        self.assertFalse((self.scratch / 'restore' / '.env').exists())

    def test_missing_attachment_file_fails(self):
        d = make_app_a(self.backups)
        (d / 'uploads' / '2.png').unlink()
        (d / 'SHA256SUMS').write_text('\n'.join(
            line for line in (d / 'SHA256SUMS').read_text().splitlines() if '2.png' not in line) + '\n')
        result = rt.run_one('app-a', self.backups, self.scratch, 'test', min_free_gb=0)
        self.assertEqual(result['status'], rt.FAIL)
        self.assertIn('1 of 3 attachments', result['detail'])

    def test_bit_rot_fails_the_checksum(self):
        d = make_app_a(self.backups)
        with open(d / 'uploads' / '1.png', 'ab') as fh:
            fh.write(b'!')
        result = rt.run_one('app-a', self.backups, self.scratch, 'test', min_free_gb=0)
        self.assertEqual(result['status'], rt.FAIL)
        self.assertIn('SHA256SUMS', result['detail'])

    def test_missing_secret_fails_without_reading_it(self):
        make_app_a(self.backups, with_env=False)
        result = rt.run_one('app-a', self.backups, self.scratch, 'test', min_free_gb=0)
        self.assertEqual(result['status'], rt.FAIL)
        self.assertIn('.env missing', result['detail'])

    def test_over_the_cap_is_unknown_not_pass(self):
        make_app_a(self.backups)
        result = rt.run_one('app-a', self.backups, self.scratch, 'test', min_free_gb=0, max_restore_gb=0)
        self.assertEqual(result['status'], rt.UNKNOWN)

    def test_no_docker_is_unknown(self):
        d = self.backups / 'mailer' / SNAP
        d.mkdir(parents=True)
        (d / 'dump.sql').write_text('CREATE TABLE subscribers (id int);\n')
        digest = hashlib.sha256((d / 'dump.sql').read_bytes()).hexdigest()
        (d / 'SHA256SUMS').write_text(f'{digest}  ./dump.sql\n')
        result = rt.run_one('mailer', self.backups, self.scratch, 'test', min_free_gb=0, docker='false')
        self.assertEqual(result['status'], rt.UNKNOWN)
        self.assertIn('docker', result['detail'])


class LatestTests(Base):
    def test_broken_latest_falls_back_to_newest_dated_with_a_note(self):
        make_app_a(self.backups, snap='2026-01-04_02-30-00')
        make_app_a(self.backups, snap=SNAP)
        link = self.backups / 'app-a' / 'latest'
        link.unlink()
        link.symlink_to('does-not-exist')
        ctx = self.ctx()
        self.assertEqual(ctx.latest('app-a').name, SNAP)
        self.assertTrue(any('latest missing or broken' in n for n in ctx.notes))

    def test_latest_pointing_outside_the_set_is_ignored(self):
        make_app_a(self.backups)
        elsewhere = self.root / '2026-02-01_00-00-00'
        elsewhere.mkdir()
        link = self.backups / 'app-a' / 'latest'
        link.unlink()
        link.symlink_to(elsewhere)
        self.assertEqual(self.ctx().latest('app-a').name, SNAP)

    def test_scratch_paths_cannot_escape(self):
        with self.assertRaises(ValueError):
            self.ctx().path('..', 'etc')


class ContainerSafetyTests(Base):
    def argv(self, **kw):
        args = dict(docker='docker', name='restoretest-mailer-db-test', image='postgres:16-alpine',
                    run_id='test', family='mailer', scratch_root=self.scratch, env={'A': '1'},
                    mounts=[(self.scratch / 'restore', '/restore')])
        args.update(kw)
        return rt.container_argv(**args)

    def test_no_network_no_pull_no_ports_read_only_mounts(self):
        argv = self.argv()
        joined = ' '.join(argv)
        self.assertIn('--network none', joined)
        self.assertIn('--pull never', joined)
        self.assertNotIn('-p', argv)
        self.assertNotIn('--publish', argv)
        self.assertTrue(argv[argv.index('--volume') + 1].endswith(':/restore:ro'))

    def test_refuses_mounts_outside_scratch(self):
        with self.assertRaises(ValueError):
            self.argv(mounts=[('/srv/backups', '/restore')])

    def test_refuses_names_that_could_be_production(self):
        with self.assertRaises(ValueError):
            self.argv(name='mailer-db')

    def test_refuses_odd_env_names(self):
        with self.assertRaises(ValueError):
            self.argv(env={'A;rm': '1'})


class SummaryTests(unittest.TestCase):
    def run_summary(self, results):
        out, err = io.StringIO(), io.StringIO()
        rc = rt.summarize(results, out=out, err=err)
        return rc, out.getvalue(), err.getvalue()

    def test_all_pass_is_zero_and_quiet_on_stderr(self):
        rc, _, err = self.run_summary([{'family': 'a', 'status': 'pass', 'detail': 'ok'}])
        self.assertEqual((rc, err), (0, ''))

    def test_unknown_is_red(self):
        rc, _, err = self.run_summary([{'family': 'a', 'status': 'pass', 'detail': 'ok'},
                                       {'family': 'b', 'status': 'unknown', 'detail': 'no docker'}])
        self.assertEqual(rc, 1)
        self.assertEqual(err.strip(), 'restore-test: 1 ok, 0 fail, 1 unknown | UNKNOWN b: no docker')


class EndToEndTests(Base):
    def test_main_runs_each_family_in_a_subprocess_and_cleans_up(self):
        make_app_a(self.backups)
        state = self.root / 'state'
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = rt.main(['--backup-root', str(self.backups), '--state-dir', str(state),
                          '--family', 'app-a', '--min-free-gb', '0', '--docker', 'false'])
        self.assertEqual(rc, 0, out.getvalue() + err.getvalue())
        status = json.loads((state / 'last-partial-run.json').read_text())
        self.assertEqual(status['counts'], {'pass': 1, 'fail': 0, 'unknown': 0})
        self.assertEqual(list(state.glob('run.*')), [])          # scratch removed
        history = (state / 'history.jsonl').read_text()
        self.assertNotIn('1.png', history)                         # no user file names

    def test_list(self):
        out = io.StringIO()
        with redirect_stdout(out):
            rt.main(['--list'])
        self.assertEqual([line.split()[0] for line in out.getvalue().splitlines()],
                         ['app-a', 'blog', 'mailer'])


if __name__ == '__main__':
    unittest.main()

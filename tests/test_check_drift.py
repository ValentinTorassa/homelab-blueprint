import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'drift'))
import check_drift as drift  # noqa: E402


class FakeTrees:
    """Stands in for the API: repo -> {path: blob sha}."""

    def __init__(self, trees):
        self.trees = trees

    def tree(self, repo):
        return self.trees[repo]


def sha(text):
    return drift.blob_sha(text.encode())


class BlobShaTests(unittest.TestCase):
    def test_matches_git_hash_object(self):
        with tempfile.NamedTemporaryFile('wb', delete=False) as f:
            f.write(b'hello\n')
        expected = subprocess.run(['git', 'hash-object', f.name], capture_output=True,
                                  text=True, check=True).stdout.strip()
        Path(f.name).unlink()
        self.assertEqual(drift.blob_sha(b'hello\n'), expected)


class DriftTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def write(self, rel, text):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def test_files_in_sync_differs_and_missing(self):
        same = self.write('sbin/a.sh', 'echo a\n')
        changed = self.write('sbin/b.sh', 'echo edited on the server\n')
        trees = FakeTrees({'r': {'scripts/a.sh': sha('echo a\n'), 'scripts/b.sh': sha('echo b\n'),
                                 'scripts/c.sh': sha('echo c\n')}})
        entry = {'repo': 'r', 'files': {'scripts/a.sh': str(same), 'scripts/b.sh': str(changed),
                                        'scripts/c.sh': str(self.root / 'sbin/c.sh'),
                                        'scripts/gone.sh': str(self.root / 'sbin/gone.sh')}}
        kinds = {p['path']: p['kind'] for p in drift.check_files(entry, trees)}
        self.assertEqual(kinds, {'scripts/b.sh': 'differs', 'scripts/c.sh': 'missing-on-server',
                                 'scripts/gone.sh': 'missing-in-git'})

    def test_tree_flags_code_only_on_server_and_ignores_runtime_dirs(self):
        self.write('svc/server.js', 'ok\n')
        self.write('svc/hotfix.js', 'added by hand on the server\n')
        self.write('svc/node_modules/x/index.js', 'vendored\n')
        self.write('svc/data/state.js', 'runtime\n')
        trees = FakeTrees({'r': {'app/server.js': sha('ok\n')}})
        problems = drift.check_tree({'repo': 'r', 'repo_dir': 'app',
                                     'server_dir': str(self.root / 'svc')}, trees)
        self.assertEqual([(p['kind'], p['path']) for p in problems],
                         [('only-on-server', 'app/hotfix.js')])

    def test_tree_docs_are_info_code_is_drift(self):
        self.write('svc/README.md', 'edited on the server\n')
        self.write('svc/lib.py', 'print(2)\n')
        trees = FakeTrees({'r': {'app/README.md': sha('from git\n'), 'app/lib.py': sha('print(1)\n'),
                                 'app/tools/new.py': sha('x\n'), 'app/ops/dev.sh': sha('y\n')}})
        problems = drift.check_tree({'repo': 'r', 'repo_dir': 'app', 'server_dir': str(self.root / 'svc'),
                                     'ignore_missing': ['ops']}, trees)
        by_path = {p['path']: (p['kind'], p['info']) for p in problems}
        self.assertEqual(by_path, {'app/README.md': ('differs', True),
                                   'app/lib.py': ('differs', False),
                                   'app/tools/new.py': ('missing-on-server', False)})

    def test_tree_at_repo_root(self):
        self.write('svc/main.py', 'print(1)\n')
        trees = FakeTrees({'r': {'main.py': sha('print(1)\n')}})
        self.assertEqual(drift.check_tree({'repo': 'r', 'repo_dir': '.',
                                           'server_dir': str(self.root / 'svc')}, trees), [])

    def test_command_compares_output(self):
        trees = FakeTrees({'r': {'cron/ops': sha('0 8 * * * job\n')}})
        self.assertEqual(drift.check_command({'repo': 'r', 'path': 'cron/ops',
                                              'command': "printf '0 8 * * * job\\n'"}, trees), [])
        self.assertEqual(drift.check_command({'repo': 'r', 'path': 'cron/ops',
                                              'command': "printf 'edited\\n'"}, trees)[0]['kind'], 'differs')

    def test_local_checkout_is_a_tree_source(self):
        repo = self.root / 'repo'
        (repo / 'server').mkdir(parents=True)
        (repo / 'server' / 'app.js').write_text('console.log(1)\n')
        git = ['git', '-C', str(repo), '-c', 'user.name=t', '-c', 'user.email=t@example.com']
        subprocess.run(['git', 'init', '-q', str(repo)], check=True)
        subprocess.run(git + ['add', '.'], check=True)
        subprocess.run(git + ['commit', '-qm', 'init'], check=True)
        trees = drift.LocalTrees({'org/app': repo})
        self.assertEqual(trees.tree('org/app'), {'server/app.js': sha('console.log(1)\n')})
        with self.assertRaises(drift.CheckError):
            trees.tree('org/other')

    def test_could_not_run_is_exit_2_not_drift(self):
        manifest = self.root / 'm.json'
        manifest.write_text(json.dumps({'tokens': {'x': str(self.root / 'no-token')},
                                        'entries': [{'name': 'n', 'type': 'files', 'repo': 'x/r',
                                                     'files': {'a': '/nonexistent'}}]}))
        with redirect_stderr(io.StringIO()):
            self.assertEqual(drift.main(['--manifest', str(manifest)]), 2)

    def test_drift_goes_to_stderr_for_the_alert(self):
        manifest = self.root / 'm.json'
        manifest.write_text('{"entries": []}')
        results = {'n': [{'kind': 'differs', 'path': 'scripts/a.sh', 'server': '/x'},
                         {'kind': 'differs', 'path': 'README.md', 'server': '/y', 'info': True}]}
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.object(drift, 'run', return_value=results), \
                redirect_stdout(stdout), redirect_stderr(stderr):
            self.assertEqual(drift.main(['--manifest', str(manifest)]), 1)
        self.assertEqual(stderr.getvalue(), 'drift: differs scripts/a.sh\n')
        self.assertIn('total: 1 file(s) out of sync, 1 informational', stdout.getvalue())

    def test_example_manifest_is_valid(self):
        manifest = json.loads((Path(drift.__file__).parent / 'drift-manifest.example.json').read_text())
        for entry in manifest['entries']:
            self.assertIn(entry['type'], drift.CHECKS, entry['name'])
            self.assertIn(entry['repo'].split('/')[0], manifest['tokens'], entry['name'])


if __name__ == '__main__':
    unittest.main()

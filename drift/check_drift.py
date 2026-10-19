#!/usr/bin/env python3
"""Drift check: is what runs on the server what is in git?

Most services on a homelab are not git checkouts: they were deployed by
copying files, and sooner or later someone (me) fixes something "just on the
server". This job answers, every night, whether the installed files still
match the default branch, and names exactly the ones that do not.

It clones nothing. For each repo it asks the GitHub API for the tree of the
default branch (one git blob SHA-1 per path) and compares it with the git blob
SHA-1 of the file on disk: sha1("blob <size>\\0" + content), the same value
`git hash-object` prints. Read-only everywhere, with a read-only token.

    check_drift.py [--manifest FILE] [--json] [--local OWNER/REPO=PATH ...]

--local reads a repo's tree from a local checkout (git ls-tree HEAD) instead
of the API: for air-gapped hosts, CI, and the demo in drift/README.md.

Exit codes, deliberately distinct:
  0  everything matches
  1  drift (Gatus goes red; stderr names the files)
  2  the check itself could not run (token, network, API, bad manifest).
     "Could not look" is not the same statement as "found drift".

The manifest lists three kinds of entries:
  files    repo path -> absolute server path, one by one
  tree     repo directory -> server directory. Also reports code files that
           exist on the server but not in git (the "only on the server" case).
           Only code counts as drift here: a README that differs is reported
           as info, since docs are not always deployed.
  command  a repo file compared with a command's output (e.g. `crontab -l`)
"""
from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
API = 'https://api.github.com'
CODE_SUFFIXES = {'.js', '.mjs', '.cjs', '.ts', '.tsx', '.jsx', '.py', '.sh', '.go',
                 '.html', '.css', '.vue', '.svelte', '.service', '.timer'}
# Runtime state, build output and secrets are never compared.
DEFAULT_EXCLUDES = ['node_modules', 'dist', 'data', '.env*', '*.log', '__pycache__',
                    '*.pyc', '*.bak*', '.git', 'media', '.venv', 'venv', 'state',
                    '*.tar.gz', '.deploy-backups', '.deployed-commit']


class CheckError(Exception):
    """The check could not run. This is not the same as drift."""


def blob_sha(data: bytes) -> str:
    return hashlib.sha1(b'blob %d\0' % len(data) + data).hexdigest()


def excluded(rel: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(part, pat) for part in rel.split('/') for pat in patterns)


def expand(path: str) -> Path:
    return Path(os.path.expanduser(path))


class GitHub:
    """Trees from the GitHub API, one request per repo per run."""

    def __init__(self, tokens: dict[str, str]):
        self.tokens = tokens              # owner -> token file
        self._trees: dict[str, dict[str, str]] = {}

    def _token(self, owner: str) -> str:
        token_file = self.tokens.get(owner)
        if token_file:
            try:
                return expand(token_file).read_text().strip()
            except OSError as exc:
                raise CheckError(f'cannot read the token for {owner}: {exc}') from exc
        if os.environ.get('GITHUB_TOKEN'):
            return os.environ['GITHUB_TOKEN']
        raise CheckError(f'no token configured for {owner}')

    def _get(self, owner: str, path: str) -> dict:
        req = urllib.request.Request(API + path, headers={
            'Authorization': f'Bearer {self._token(owner)}',
            'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28'})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            raise CheckError(f'GitHub API {path}: {exc}') from exc

    def tree(self, repo: str) -> dict[str, str]:
        """path -> blob sha on the default branch (regular files only)."""
        if repo not in self._trees:
            owner = repo.split('/')[0]
            branch = self._get(owner, f'/repos/{repo}')['default_branch']
            data = self._get(owner, f'/repos/{repo}/git/trees/{branch}?recursive=1')
            if data.get('truncated'):
                raise CheckError(f'{repo}: tree truncated by the API')
            self._trees[repo] = {e['path']: e['sha'] for e in data['tree']
                                 if e['type'] == 'blob' and e['mode'] != '120000'}
        return self._trees[repo]


class LocalTrees:
    """Same interface, from local checkouts; falls back to another source."""

    def __init__(self, checkouts: dict[str, Path], fallback=None):
        self.checkouts = checkouts
        self.fallback = fallback

    def tree(self, repo: str) -> dict[str, str]:
        if repo not in self.checkouts:
            if self.fallback is None:
                raise CheckError(f'no local checkout for {repo}')
            return self.fallback.tree(repo)
        try:
            out = subprocess.run(['git', '-C', str(self.checkouts[repo]), 'ls-tree', '-r', '-z',
                                  '--full-tree', 'HEAD'], capture_output=True, check=True).stdout
        except (OSError, subprocess.CalledProcessError) as exc:
            raise CheckError(f'{repo}: git ls-tree failed: {exc}') from exc
        tree = {}
        for record in out.decode().split('\0'):
            if not record:
                continue
            meta, path = record.split('\t', 1)
            mode, kind, sha = meta.split()
            if kind == 'blob' and mode != '120000':
                tree[path] = sha
        return tree


def check_files(entry: dict, trees) -> list[dict]:
    tree, problems = trees.tree(entry['repo']), []
    for repo_path, server_path in entry['files'].items():
        server = expand(server_path)
        if repo_path not in tree:
            problems.append({'kind': 'missing-in-git', 'path': repo_path, 'server': str(server)})
        elif not server.exists():
            problems.append({'kind': 'missing-on-server', 'path': repo_path, 'server': str(server)})
        elif blob_sha(server.read_bytes()) != tree[repo_path]:
            problems.append({'kind': 'differs', 'path': repo_path, 'server': str(server)})
    return problems


def is_code(path: str) -> bool:
    return Path(path).suffix in CODE_SUFFIXES


def check_tree(entry: dict, trees) -> list[dict]:
    tree, problems = trees.tree(entry['repo']), []
    repo_dir = entry.get('repo_dir', '').strip('/')
    prefix = '' if repo_dir in ('', '.') else repo_dir + '/'
    root = expand(entry['server_dir'])
    excludes = DEFAULT_EXCLUDES + entry.get('exclude', [])
    ignore_missing = entry.get('ignore_missing', [])
    if not root.is_dir():
        return [{'kind': 'missing-on-server', 'path': repo_dir or '.', 'server': str(root)}]
    tracked = {p[len(prefix):]: sha for p, sha in tree.items() if p.startswith(prefix)}
    for rel, sha in sorted(tracked.items()):
        if excluded(rel, excludes):
            continue
        server = root / rel
        if not server.exists():
            if not excluded(rel, ignore_missing):
                problems.append({'kind': 'missing-on-server', 'path': prefix + rel,
                                 'server': str(server), 'info': not is_code(rel)})
        elif blob_sha(server.read_bytes()) != sha:
            problems.append({'kind': 'differs', 'path': prefix + rel,
                             'server': str(server), 'info': not is_code(rel)})
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        dirnames[:] = sorted(d for d in dirnames
                             if not excluded(os.path.normpath(os.path.join(rel_dir, d)), excludes))
        for name in sorted(filenames):
            rel = os.path.normpath(os.path.join(rel_dir, name))
            if is_code(name) and rel not in tracked and not excluded(rel, excludes):
                problems.append({'kind': 'only-on-server', 'path': prefix + rel,
                                 'server': str(root / rel)})
    return problems


def check_command(entry: dict, trees) -> list[dict]:
    tree = trees.tree(entry['repo'])
    if entry['path'] not in tree:
        return [{'kind': 'missing-in-git', 'path': entry['path'], 'server': entry['command']}]
    try:
        out = subprocess.run(entry['command'], shell=True, capture_output=True,
                             timeout=30, check=True).stdout
    except (subprocess.SubprocessError, OSError) as exc:
        raise CheckError(f'{entry["command"]}: {exc}') from exc
    if blob_sha(out) != tree[entry['path']]:
        return [{'kind': 'differs', 'path': entry['path'], 'server': entry['command']}]
    return []


CHECKS = {'files': check_files, 'tree': check_tree, 'command': check_command}


def run(manifest: dict, trees) -> dict[str, list[dict]]:
    return {entry['name']: CHECKS[entry['type']](entry, trees) for entry in manifest['entries']}


def parse_local(values: list[str]) -> dict[str, Path]:
    out = {}
    for value in values or []:
        repo, sep, path = value.partition('=')
        if not sep or not repo or not path:
            raise CheckError(f'--local expects OWNER/REPO=PATH, got {value!r}')
        out[repo] = expand(path)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--manifest', default=str(HERE / 'drift-manifest.example.json'))
    parser.add_argument('--json', action='store_true', help='machine-readable output')
    parser.add_argument('--local', action='append', metavar='OWNER/REPO=PATH',
                        help='read this repo from a local checkout instead of the API')
    args = parser.parse_args(argv)
    try:
        manifest = json.loads(Path(args.manifest).read_text())
        trees = LocalTrees(parse_local(args.local), fallback=GitHub(manifest.get('tokens', {})))
        results = run(manifest, trees)
    except (CheckError, OSError, json.JSONDecodeError, KeyError) as exc:
        print(f'drift check could not run: {exc}', file=sys.stderr)
        return 2
    drift = sum(1 for ps in results.values() for p in ps if not p.get('info'))
    info = sum(1 for ps in results.values() for p in ps if p.get('info'))
    if args.json:
        print(json.dumps({'drift': drift, 'info': info, 'results': results}, indent=2))
    else:
        for name, problems in results.items():
            real = [p for p in problems if not p.get('info')]
            extra = f' (+{len(problems) - len(real)} info)' if len(problems) > len(real) else ''
            print(f'{"OK   " if not real else "DRIFT"} {name}: {len(real)}{extra}')
            for p in problems:
                tag = 'info ' if p.get('info') else ''
                print(f'        {tag}{p["kind"]:<18} {p["path"]}  ({p["server"]})')
        print(f'total: {drift} file(s) out of sync, {info} informational')
    sys.stdout.flush()
    # run-and-report.sh sends only the tail of stderr to Gatus; without this the
    # alert would say "exit 1" and nothing about which files are out of sync.
    for problems in results.values():
        for p in problems:
            if not p.get('info'):
                print(f'drift: {p["kind"]} {p["path"]}', file=sys.stderr)
    return 1 if drift else 0


if __name__ == '__main__':
    raise SystemExit(main())

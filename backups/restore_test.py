#!/usr/bin/env python3
"""Weekly restore test: prove that the latest backups can actually be restored.

The nightly jobs report that they RAN. This proves, once a week, that what they
left behind is usable. For every family it copies the `latest` snapshot into a
throwaway scratch directory and asserts something that is only true if the
backup is good: the checksum manifest matches, the SQLite copy passes
integrity_check and has rows, every attachment row has its file, a SQL dump
loads into a brand-new database inside a container with no network.

Rules every family check follows (tests/test_restore_test.py pins them):

- Backups are read, never written. Everything is copied to scratch first and
  the COPY is opened. Not even SQLite is opened in place: opening it can create
  -journal/-shm files next to it. The systemd unit adds ReadOnlyPaths= on top.
- Credentials of other systems (.env, *.token, ...) are only stat()-ed: present
  and non-empty. Never read, never copied.
- Throwaway containers are named restoretest-*, run with --network none, no
  published ports, --pull never, resource limits, and mount only directories
  inside the scratch. Nothing here stops or removes a container whose name does
  not start with restoretest-.
- Disk caps: a family that does not fit under --max-restore-gb, or that would
  leave less than --min-free-gb free, is UNKNOWN instead of risking the host.
- Results carry counts, sizes and hashes; never user file names or content.

Outcome per family: pass, fail, or unknown (could not be tested safely, or a
host prerequisite such as docker is missing). UNKNOWN IS RED: untested is not
green.

    restore_test.py --backup-root /srv/backups               # every family
    restore_test.py --backup-root DIR --family app-a         # one (repeatable)
    restore_test.py --list
    restore_test.py --cleanup        # leftovers of a run that was killed

Each family runs in its own process under a timeout, in its own scratch that
is deleted as soon as it finishes. Exit 0 only if every selected family
passes. The last stderr line summarizes what did not pass; run-and-report.sh
sends exactly that tail to Gatus. Standard library only.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import fnmatch
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PASS, FAIL, UNKNOWN = 'pass', 'fail', 'unknown'
GiB = 1024 ** 3
SNAP_RE = re.compile(r'^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}$')
CONTAINER_NAME_RE = re.compile(r'^restoretest-[a-z0-9][a-z0-9-]{0,100}$')
ENV_KEY_RE = re.compile(r'^[A-Z_][A-Z0-9_]*$')
LABEL = 'homelab.restore-test'

# Credentials of other systems: stat only. Matched on the file name.
SECRET_NAMES = {'.env', 'env', 'config.php'}
SECRET_GLOBS = ('*.env', '*.token', '*.pass', '*.key', '*.pem', '*credentials*.json', '*token*.json')


class Failed(Exception):
    """The backup could not be restored, or an assertion did not hold."""


class Unknown(Exception):
    """It could not be tested without risk, or a host prerequisite is missing."""


def is_secret(name: str) -> bool:
    return name in SECRET_NAMES or any(fnmatch.fnmatch(name, g) for g in SECRET_GLOBS)


def clean_line(text: str, limit: int = 160) -> str:
    """One error line without data: cut at the first quote (where the value that
    failed to load usually starts) and drop control characters."""
    text = re.split(r'["\'`]', text or '', maxsplit=1)[0]
    return re.sub(r'[\x00-\x1f\x7f]+', ' ', text).strip()[:limit]


def file_digest(path: Path, algo: str = 'sha256') -> str:
    h = hashlib.new(algo)
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def walk_files(root: Path):
    """(path, size) of every regular file under root, never following symlinks."""
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        for name in sorted(filenames):
            p = Path(dirpath) / name
            st = p.lstat()
            if stat.S_ISREG(st.st_mode):
                yield p, st.st_size


class Context:
    """One family in one run: paths, caps, facts and notes."""

    def __init__(self, family, backup_root, scratch, run_id='manual',
                 max_restore_gb=12.0, min_free_gb=20.0, docker='docker'):
        self.family = family
        self.backup_root = Path(backup_root)
        self.scratch = Path(scratch)
        self.run_id = run_id
        self.max_restore = max_restore_gb * GiB
        self.min_free = min_free_gb * GiB
        self.docker = docker
        self.reserved = 0
        self.facts: dict = {}
        self.notes: list[str] = []
        self.snapshots: dict[str, str] = {}

    def fact(self, key, value):
        self.facts[key] = value

    def note(self, text):
        self.notes.append(text)

    def check(self, condition, message):
        if not condition:
            raise Failed(message)

    def path(self, *parts) -> Path:
        """A path inside the family's scratch; anything else is a bug."""
        p = self.scratch.joinpath(*parts)
        root = self.scratch.resolve()
        if root != p.resolve() and root not in p.resolve().parents:
            raise ValueError(f'path outside scratch: {p}')
        return p

    def latest(self, setname: str) -> Path:
        """The snapshot `latest` points to; if it is missing or broken, the newest dated one."""
        base = self.backup_root / setname
        try:
            if not base.is_dir():
                raise Failed(f'{setname}: no such backup set')
            target = None
            link = base / 'latest'
            if link.is_symlink():
                t = link.resolve()
                if t.parent == base.resolve() and SNAP_RE.match(t.name) and t.is_dir():
                    target = t
            if target is None:
                dated = sorted(d for d in base.iterdir()
                               if d.is_dir() and not d.is_symlink() and SNAP_RE.match(d.name))
                if not dated:
                    raise Failed(f'{setname}: no snapshot at all')
                target = dated[-1]
                self.note(f'{setname}: latest missing or broken; using {target.name}')
            os.listdir(target)
        except PermissionError as exc:
            raise Unknown(f'{setname}: not readable as uid {os.geteuid()} (the weekly run is root)') from exc
        self.snapshots[setname] = target.name
        age = time.time() - time.mktime(time.strptime(target.name, '%Y-%m-%d_%H-%M-%S'))
        self.fact(f'{setname}.age_hours', round(age / 3600, 1))
        return target

    def require_secret(self, path: Path):
        """The credential is there and not empty, without opening it."""
        try:
            st = path.lstat()
        except FileNotFoundError:
            raise Failed(f'{path.name} missing from the snapshot (the service cannot start without it)') from None
        if not stat.S_ISREG(st.st_mode) or st.st_size == 0:
            raise Failed(f'{path.name} is empty or not a regular file')

    def ensure_space(self, nbytes: int, what: str):
        total = self.reserved + nbytes
        if total > self.max_restore:
            raise Unknown(f'{what}: needs {total / GiB:.1f} GiB, over the '
                          f'{self.max_restore / GiB:.1f} GiB cap (--max-restore-gb)')
        self.scratch.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(self.scratch).free
        if free - nbytes < self.min_free:
            raise Unknown(f'{what}: would leave {(free - nbytes) / GiB:.1f} GiB free, '
                          f'under the {self.min_free / GiB:.1f} GiB floor (--min-free-gb)')
        self.reserved = total


# -- building blocks -----------------------------------------------------------------

def restore_tree(ctx: Context, snap: Path, dst_name='restore') -> Path:
    """Copy the snapshot into scratch (minus credentials) and check it arrived whole.

    Reading every byte is part of the test: it proves the disk returns the data."""
    src_files = [(p, n) for p, n in walk_files(snap) if not is_secret(p.name)]
    size = sum(n for _, n in src_files)
    ctx.ensure_space(size, f'copy of {ctx.family}')
    dst = ctx.path(dst_name)
    try:
        for p, _ in src_files:
            target = dst / p.relative_to(snap)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, target, follow_symlinks=False)
    except PermissionError as exc:
        raise Unknown(f'snapshot not readable as uid {os.geteuid()}') from exc
    except OSError as exc:
        raise Failed(f'could not read the whole snapshot: {exc.strerror or exc}') from exc
    got = list(walk_files(dst))
    ctx.check((len(got), sum(n for _, n in got)) == (len(src_files), size),
              f'the copy differs: {len(got)} files/{sum(n for _, n in got)} B '
              f'of {len(src_files)}/{size}')
    ctx.fact('restored_files', len(src_files))
    ctx.fact('restored_bytes', size)
    return dst


def verify_sha256sums(ctx: Context, root: Path, sums_name='SHA256SUMS'):
    """Every `<sha256>  ./path` line must exist under root and match. Skips the
    manifest itself and credentials (not copied, never read)."""
    sums = root / sums_name
    if not sums.is_file():
        raise Failed(f'{sums_name} missing from the snapshot')
    checked, bad = 0, []
    for line in sums.read_text(encoding='utf-8').splitlines():
        if not line.strip():
            continue
        digest, _, name = line.partition('  ')
        rel = Path(name[2:] if name.startswith('./') else name)
        if rel.is_absolute() or '..' in rel.parts or not re.fullmatch(r'[0-9a-f]{64}', digest):
            raise Failed(f'{sums_name}: malformed line')
        if rel.name == sums_name or is_secret(rel.name):
            continue
        p = root / rel
        if not p.is_file():
            bad.append('missing')
        elif file_digest(p) != digest:
            bad.append('differs')
        checked += 1
    ctx.fact('sha256.checked', checked)
    ctx.check(checked > 0, f'{sums_name} lists no verifiable file')
    ctx.check(not bad, f'{sums_name}: {len(bad)} file(s) do not match ({bad.count("missing")} missing)')


def sqlite_check(ctx: Context, db: Path, tables, min_rows: dict, label: str) -> dict:
    """integrity_check, expected tables and minimum rows, on a COPY, read-only."""
    try:
        con = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
        try:
            ok = con.execute('PRAGMA integrity_check').fetchall()
            present = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            missing = sorted(set(tables) - present)
            ctx.check(ok == [('ok',)], f'{label}: integrity_check did not return ok')
            ctx.check(not missing, f'{label}: missing tables {", ".join(missing)}')
            counts = {t: con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in sorted(tables)}
        finally:
            con.close()
    except sqlite3.DatabaseError as exc:
        raise Failed(f'{label}: does not open as SQLite ({clean_line(str(exc))})') from exc
    for table, minimum in min_rows.items():
        ctx.check(counts[table] >= minimum, f'{label}: {table} has {counts[table]} rows (minimum {minimum})')
    for table, n in counts.items():
        ctx.fact(f'{label}.{table}', n)
    return counts


def container_argv(docker, name, image, run_id, family, scratch_root, env, mounts, memory='1g'):
    """The `docker run` line of a throwaway container. All of the test's safety
    with respect to production rests on these flags, so they are validated here."""
    if not CONTAINER_NAME_RE.match(name):
        raise ValueError(f'container name not allowed: {name}')
    root = Path(scratch_root).resolve()
    argv = [docker, 'run', '--detach', '--name', name,
            '--label', f'{LABEL}=1', '--label', f'{LABEL}.run={run_id}',
            '--label', f'{LABEL}.family={family}',
            '--network', 'none', '--pull', 'never', '--restart', 'no',
            '--memory', memory, '--memory-swap', memory, '--cpus', '2',
            '--pids-limit', '512', '--security-opt', 'no-new-privileges']
    for key, value in env.items():
        if not ENV_KEY_RE.match(key):
            raise ValueError(f'invalid variable name: {key}')
        argv += ['--env', f'{key}={value}']
    for host, inside in mounts:
        h = Path(host).resolve()
        if h != root and root not in h.parents:
            raise ValueError(f'mount outside scratch: {host}')
        if ':' in str(h) or ':' in inside:
            raise ValueError(f'mount path contains ":": {host}')
        argv += ['--volume', f'{h}:{inside}:ro']
    argv.append(image)
    return argv


def docker_ok(docker: str) -> bool:
    try:
        return subprocess.run([docker, 'info'], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


@contextlib.contextmanager
def throwaway(ctx: Context, role: str, image: str, env: dict, mounts):
    """A network-less container that is removed whatever happens."""
    d = ctx.docker
    if not docker_ok(d):
        raise Unknown('docker is not available on this host')
    if subprocess.run([d, 'image', 'inspect', image], capture_output=True).returncode != 0:
        raise Unknown(f'image {image} is not on the host (the test never pulls)')
    name = re.sub(r'[^a-z0-9-]', '-', f'restoretest-{ctx.family}-{role}-{ctx.run_id}'.lower())
    argv = container_argv(d, name, image, ctx.run_id, ctx.family, ctx.scratch, env, mounts)
    subprocess.run(argv, capture_output=True, check=True, timeout=60)
    try:
        yield name
    finally:
        subprocess.run([d, 'rm', '--force', '--volumes', name], capture_output=True)


def wait_for(probe, timeout: float, what: str):
    deadline = time.monotonic() + timeout
    while not probe():
        if time.monotonic() > deadline:
            raise Failed(f'{what}: no answer in {timeout:.0f}s')
        time.sleep(2)


# -- families ------------------------------------------------------------------------
# Each check restores one family and asserts what "usable" means for it. The
# docstring says what is proven. Replace these three with your own.

FAMILIES: dict[str, tuple] = {}


def family(name, timeout=1200):
    def register(fn):
        FAMILIES[name] = (fn, timeout)
        return fn
    return register


@family('app-a')
def app_a(ctx: Context):
    """SHA256SUMS matches; data/app.db passes integrity_check with items; every
    attachments row has uploads/<id>.<ext> with the size the database records;
    the .env is present (stat only)."""
    snap = ctx.latest('app-a')
    ctx.require_secret(snap / '.env')
    restored = restore_tree(ctx, snap)
    verify_sha256sums(ctx, restored)
    db = restored / 'data' / 'app.db'
    sqlite_check(ctx, db, {'items', 'attachments'}, {'items': 1}, 'db')
    con = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
    try:
        rows = con.execute('SELECT id, ext, size_bytes FROM attachments').fetchall()
    finally:
        con.close()
    missing = 0
    for att_id, ext, size in rows:
        p = restored / 'uploads' / f'{att_id}.{ext}'
        if '/' in f'{att_id}{ext}' or not p.is_file() or p.stat().st_size != size:
            missing += 1
    ctx.fact('attachments.ok', len(rows) - missing)
    ctx.check(missing == 0, f'{missing} of {len(rows)} attachments have no matching file in uploads/')


@family('blog')
def blog(ctx: Context):
    """SHA256SUMS matches and the posts database is usable: integrity_check,
    expected tables, at least one published post."""
    restored = restore_tree(ctx, ctx.latest('blog'))
    verify_sha256sums(ctx, restored)
    sqlite_check(ctx, restored / 'blog.db', {'posts'}, {'posts': 1}, 'db')


@family('mailer', timeout=900)
def mailer(ctx: Context):
    """dump.sql loads with zero errors (ON_ERROR_STOP) into a brand-new Postgres
    in a container with no network, and the subscribers table has rows."""
    snap = ctx.latest('mailer')
    restored = restore_tree(ctx, snap)
    verify_sha256sums(ctx, restored)
    if not (restored / 'dump.sql').is_file():
        raise Failed('dump.sql missing from the snapshot')
    image = os.environ.get('RT_POSTGRES_IMAGE', 'postgres:16-alpine')
    env = {'POSTGRES_PASSWORD': secrets.token_hex(16)}   # throwaway, never leaves the box
    with throwaway(ctx, 'db', image, env, [(restored, '/restore')]) as name:
        d = ctx.docker

        def ready():
            return subprocess.run([d, 'exec', name, 'pg_isready', '-U', 'postgres'],
                                  capture_output=True).returncode == 0
        wait_for(ready, 120, 'postgres')
        load = subprocess.run([d, 'exec', name, 'psql', '-U', 'postgres', '-q',
                               '-v', 'ON_ERROR_STOP=1', '-f', '/restore/dump.sql'],
                              capture_output=True, text=True)
        ctx.check(load.returncode == 0, f'dump did not load: {clean_line(load.stderr)}')
        count = subprocess.run([d, 'exec', name, 'psql', '-U', 'postgres', '-tAc',
                                'SELECT count(*) FROM subscribers'], capture_output=True, text=True)
        ctx.check(count.returncode == 0, 'subscribers table missing after the load')
        n = int(count.stdout.strip() or 0)
        ctx.fact('subscribers', n)
        ctx.check(n > 0, 'subscribers is empty')


# -- running ---------------------------------------------------------------------------

def run_one(name, backup_root, scratch, run_id='manual', **caps) -> dict:
    fn, _ = FAMILIES[name]
    ctx = Context(name, backup_root, scratch, run_id, **caps)
    start = time.monotonic()
    try:
        fn(ctx)
        status, detail = PASS, 'ok'
    except Failed as exc:
        status, detail = FAIL, str(exc)
    except Unknown as exc:
        status, detail = UNKNOWN, str(exc)
    except PermissionError:
        status, detail = UNKNOWN, f'not readable as uid {os.geteuid()}'
    except Exception as exc:  # a bug in the check is "not proven" too
        status, detail = FAIL, f'unexpected error: {type(exc).__name__}: {clean_line(str(exc))}'
    return {'family': name, 'status': status, 'detail': detail,
            'seconds': round(time.monotonic() - start, 1), 'snapshots': ctx.snapshots,
            'facts': ctx.facts, 'notes': ctx.notes}


def remove_leftover_containers(docker: str):
    """Containers of an earlier run that a timeout or power cut left behind.
    Both the label AND the restoretest- name are required: never production."""
    try:
        out = subprocess.run([docker, 'ps', '-a', '--filter', f'label={LABEL}=1',
                              '--format', '{{.ID}} {{.Names}}'],
                             capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return
    for line in out.splitlines():
        cid, _, name = line.partition(' ')
        if CONTAINER_NAME_RE.match(name):
            subprocess.run([docker, 'rm', '--force', '--volumes', cid], capture_output=True)


def run_all(names, backup_root, state_dir: Path, args) -> list[dict]:
    """Each family in its own process, under its timeout, in its own scratch."""
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    for stale in state_dir.glob('run.*'):
        shutil.rmtree(stale, ignore_errors=True)
    remove_leftover_containers(args.docker)
    run_dir = Path(tempfile.mkdtemp(prefix='run.', dir=state_dir))
    run_id = time.strftime('%Y%m%d%H%M%S') + f'-{os.getpid()}'
    results = []
    try:
        for name in names:
            scratch, result = run_dir / name, run_dir / f'{name}.json'
            scratch.mkdir(mode=0o700)
            timeout = args.timeout or FAMILIES[name][1]
            cmd = [sys.executable, os.path.abspath(__file__), '--one', name,
                   '--backup-root', str(backup_root), '--scratch', str(scratch),
                   '--result', str(result), '--run-id', run_id,
                   '--max-restore-gb', str(args.max_restore_gb),
                   '--min-free-gb', str(args.min_free_gb), '--docker', args.docker]
            start = time.monotonic()
            try:
                proc = subprocess.run(cmd, timeout=timeout)
                detail = f'the check exited {proc.returncode} without a result'
            except subprocess.TimeoutExpired:
                detail = f'did not finish in {timeout}s (timeout)'
            try:
                results.append(json.loads(result.read_text()))
            except (OSError, ValueError):
                results.append({'family': name, 'status': FAIL, 'detail': detail,
                                'seconds': round(time.monotonic() - start, 1), 'facts': {}, 'notes': []})
            shutil.rmtree(scratch, ignore_errors=True)     # free the disk before the next one
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)
        remove_leftover_containers(args.docker)
    return results


def summarize(results, status_file=None, history_file=None, out=None, err=None) -> int:
    out, err = out or sys.stdout, err or sys.stderr
    for r in results:
        print(f'{r["status"]:<7} {r["family"]:<12} {r.get("seconds", "-")}s  {r["detail"]}', file=out)
        facts = ' '.join(f'{k}={v}' for k, v in r.get('facts', {}).items())
        if facts:
            print(f'        {facts}', file=out)
        for n in r.get('notes', []):
            print(f'        note: {n}', file=out)
    counts = {s: sum(1 for r in results if r['status'] == s) for s in (PASS, FAIL, UNKNOWN)}
    record = {'finished_at': dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds'),
              'counts': counts, 'results': results}
    if status_file:
        tmp = Path(f'{status_file}.tmp')
        tmp.write_text(json.dumps(record, indent=2) + '\n')
        os.replace(tmp, status_file)
    if history_file:
        with open(history_file, 'a', encoding='utf-8') as fh:
            fh.write(json.dumps(record) + '\n')
    head = f'restore-test: {counts[PASS]} ok, {counts[FAIL]} fail, {counts[UNKNOWN]} unknown'
    print(head, file=out)
    bad = [r for r in results if r['status'] != PASS]
    if not bad:
        return 0
    # The last stderr line is what reaches the alert, so it names what is red.
    out.flush()
    parts = [f'{r["status"].upper()} {r["family"]}: {r["detail"][:90]}' for r in bad]
    print(' | '.join([head, *parts])[:240], file=err)
    return 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--backup-root', default=os.environ.get('RT_BACKUP_ROOT', '/srv/backups'))
    ap.add_argument('--state-dir', default=os.environ.get('RT_STATE_DIR', '/var/lib/restore-test'))
    ap.add_argument('--family', action='append', help='only this family (repeatable)')
    ap.add_argument('--list', action='store_true', help='list families and their timeouts')
    ap.add_argument('--cleanup', action='store_true',
                    help='remove scratch and containers left by a killed run, unless one is running')
    ap.add_argument('--timeout', type=int, help='override every per-family timeout (seconds)')
    ap.add_argument('--max-restore-gb', type=float, default=12.0)
    ap.add_argument('--min-free-gb', type=float, default=20.0)
    ap.add_argument('--docker', default=os.environ.get('RT_DOCKER', 'docker'))
    # internal: one family in this process (used by run_all)
    ap.add_argument('--one', help=argparse.SUPPRESS)
    ap.add_argument('--scratch', help=argparse.SUPPRESS)
    ap.add_argument('--result', help=argparse.SUPPRESS)
    ap.add_argument('--run-id', default='manual', help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    if args.list:
        for name, (_, timeout) in FAMILIES.items():
            print(name, timeout)
        return 0
    caps = dict(max_restore_gb=args.max_restore_gb, min_free_gb=args.min_free_gb, docker=args.docker)
    if args.one:
        if args.one not in FAMILIES or not args.scratch or not re.fullmatch(r'[a-z0-9-]+', args.run_id):
            ap.error('--one needs a known family, --scratch and a safe --run-id')
        result = run_one(args.one, args.backup_root, args.scratch, args.run_id, **caps)
        if args.result:
            Path(args.result).write_text(json.dumps(result) + '\n')
        return 0

    names = args.family or list(FAMILIES)
    unknown = [n for n in names if n not in FAMILIES]
    if unknown:
        ap.error(f'unknown family: {", ".join(unknown)} (see --list)')
    os.umask(0o077)   # scratch, state and decrypted data stay private
    state_dir = Path(args.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(state_dir / '.lock', 'w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if args.cleanup:     # a live run (e.g. a manual one) is never touched
                print('restore-test: a run is in progress; nothing cleaned')
                return 0
            print('restore-test: another run is in progress', file=sys.stderr)
            return 1
        if args.cleanup:
            for stale in state_dir.glob('run.*'):
                shutil.rmtree(stale, ignore_errors=True)
            remove_leftover_containers(args.docker)
            return 0
        results = run_all(names, args.backup_root, state_dir, args)
        # A hand-picked subset never overwrites the weekly status.
        status = state_dir / ('last-run.json' if not args.family else 'last-partial-run.json')
        return summarize(results, status, state_dir / 'history.jsonl')


if __name__ == '__main__':
    sys.exit(main())

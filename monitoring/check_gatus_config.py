#!/usr/bin/env python3
"""Lint a Gatus config for the mistakes Gatus accepts silently.

Gatus has no "validate and exit" mode: the real test is starting it. This
catches, without Docker, the things that start fine and then quietly do
nothing:

- unknown top-level keys (a typo like `endpoint:` leaves the dashboard empty);
- endpoints without name/url/conditions, or a condition without an operator;
- intervals, timeouts and heartbeats that are not Go durations (`8d` stops the
  service: time.Duration has no days), and heartbeats under 10s;
- `alerts: - type: X` with no provider X under `alerting:` (dropped silently);
- two endpoints that map to the same Gatus key;
- a job that reports to a key with no external endpoint in the jobs group.
  Gatus answers 404, the report is lost, and the job LOOKS monitored.

    check_gatus_config.py [--config FILE] [--jobs-group Jobs] [--scan DIR ...]

Exit 0 clean, 1 problems found, 2 the config could not be read. Needs PyYAML.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
DEFAULT_CONFIG = HERE / 'gatus' / 'config.example.yaml'

TOP_LEVEL = {
    'metrics', 'storage', 'alerting', 'endpoints', 'external-endpoints', 'suites',
    'security', 'disable-monitoring-lock', 'skip-invalid-config-update', 'web', 'ui',
    'maintenance', 'remote', 'connectivity', 'concurrency', 'announcements', 'tunneling',
}
GO_UNITS = {'ns': 1e-9, 'us': 1e-6, 'µs': 1e-6, 'ms': 1e-3, 's': 1, 'm': 60, 'h': 3600}
GO_TERM = re.compile(r'(\d+(?:\.\d*)?|\.\d+)(ns|us|µs|ms|s|m|h)')
OPERATORS = ('==', '!=', '<=', '>=', '<', '>')
MIN_HEARTBEAT_SECONDS = 10

# How a job names the endpoint it reports to (see monitoring/jobs/).
REPORT_RE = re.compile(r'run-and-report\.sh\s+([a-z0-9][a-z0-9-]*)')
SCAN_SKIP_DIRS = {'.git', 'tests', '__pycache__'}


def is_job_definition(path: Path) -> bool:
    """Jobs are defined in cron files and systemd units; scripts and docs only
    mention the wrapper in prose and usage examples."""
    return path.suffix == '.service' or 'cron' in path.name


def go_duration(value) -> float:
    """Seconds in a Go duration (`2m`, `1h30m`, `192h`), or ValueError."""
    text = str(value).strip()
    if not text:
        raise ValueError('empty duration')
    if text == '0':
        return 0.0
    pos, total = 0, 0.0
    while pos < len(text):
        m = GO_TERM.match(text, pos)
        if not m:
            raise ValueError(f'not a Go duration: {text!r}')
        total += float(m.group(1)) * GO_UNITS[m.group(2)]
        pos = m.end()
    return total


def gatus_key(group: str | None, name: str) -> str:
    """The key Gatus uses in /api/v1/endpoints/<key>/... ("<group>_<name>")."""
    def sanitize(s: str) -> str:
        s = s.strip().lower()
        for ch in '/_.,# +&':
            s = s.replace(ch, '-')
        return s
    return f'{sanitize(group or "")}_{sanitize(name)}'


def check_config(cfg: dict) -> list[str]:
    errors = []
    for key in cfg:
        if key not in TOP_LEVEL:
            errors.append(f'unknown top-level key {key!r} (Gatus ignores it silently)')
    providers = set((cfg.get('alerting') or {}).keys())
    seen: dict[str, str] = {}

    def common(ep: dict, where: str):
        name = ep.get('name')
        if not name:
            errors.append(f'{where}: missing name')
            return
        key = gatus_key(ep.get('group'), str(name))
        if key in seen:
            errors.append(f'{where}: same key {key!r} as {seen[key]}')
        seen[key] = where
        for alert in ep.get('alerts') or []:
            if alert.get('type') not in providers:
                errors.append(f'{where}: alert type {alert.get("type")!r} has no provider '
                              f'under alerting: (Gatus drops it)')

    for i, ep in enumerate(cfg.get('endpoints') or []):
        where = f'endpoints[{i}] {ep.get("name", "?")!r}'
        common(ep, where)
        if not ep.get('url'):
            errors.append(f'{where}: missing url')
        conditions = ep.get('conditions') or []
        if not conditions:
            errors.append(f'{where}: no conditions (it can never fail)')
        for c in conditions:
            if not any(op in str(c) for op in OPERATORS):
                errors.append(f'{where}: condition without an operator: {c!r}')
        for field, value in (('interval', ep.get('interval')),
                             ('client.timeout', (ep.get('client') or {}).get('timeout'))):
            if value is not None:
                try:
                    go_duration(value)
                except ValueError as exc:
                    errors.append(f'{where}: {field}: {exc}')

    for i, ep in enumerate(cfg.get('external-endpoints') or []):
        where = f'external-endpoints[{i}] {ep.get("name", "?")!r}'
        common(ep, where)
        if not ep.get('token'):
            errors.append(f'{where}: missing token')
        interval = (ep.get('heartbeat') or {}).get('interval')
        if interval is None:
            errors.append(f'{where}: no heartbeat.interval (a job that stops running stays green)')
            continue
        try:
            seconds = go_duration(interval)
        except ValueError as exc:
            errors.append(f'{where}: heartbeat.interval: {exc}')
            continue
        if 0 < seconds < MIN_HEARTBEAT_SECONDS:
            errors.append(f'{where}: heartbeat.interval under {MIN_HEARTBEAT_SECONDS}s')
    return errors


def job_endpoint_names(cfg: dict, jobs_group: str) -> set[str]:
    prefix = gatus_key(jobs_group, '')
    return {gatus_key(ep.get('group'), str(ep['name']))[len(prefix):]
            for ep in cfg.get('external-endpoints') or []
            if ep.get('name') and gatus_key(ep.get('group'), '') == prefix}


def reported_keys(roots: list[Path]) -> dict[str, list[str]]:
    """{key: ['file:line', ...]} for every `run-and-report.sh <key>` in job definitions."""
    found: dict[str, list[str]] = {}
    for root in roots:
        for path in sorted(root.rglob('*')):
            if (not path.is_file() or not is_job_definition(path)
                    or SCAN_SKIP_DIRS & set(path.relative_to(root).parts)):
                continue
            try:
                lines = path.read_text(encoding='utf-8').splitlines()
            except (UnicodeDecodeError, OSError):
                continue
            for n, line in enumerate(lines, 1):
                if line.lstrip().startswith('#'):
                    continue
                for key in REPORT_RE.findall(line):
                    found.setdefault(key, []).append(f'{path.relative_to(root)}:{n}')
    return found


def check_job_keys(cfg: dict, jobs_group: str, roots: list[Path]) -> list[str]:
    known = job_endpoint_names(cfg, jobs_group)
    return [f'{", ".join(where)}: reports to {key!r}, but there is no external endpoint '
            f'{key!r} in group {jobs_group!r} (Gatus would answer 404)'
            for key, where in sorted(reported_keys(roots).items()) if key not in known]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--config', default=str(DEFAULT_CONFIG))
    ap.add_argument('--jobs-group', default='Jobs')
    ap.add_argument('--scan', action='append', type=Path,
                    help='directory with cron files and systemd units to scan for job keys '
                         '(default: the repo root)')
    args = ap.parse_args(argv)
    try:
        cfg = yaml.safe_load(Path(args.config).read_text(encoding='utf-8')) or {}
    except (OSError, yaml.YAMLError) as exc:
        print(f'cannot read {args.config}: {exc}', file=sys.stderr)
        return 2
    errors = check_config(cfg) + check_job_keys(cfg, args.jobs_group, args.scan or [REPO])
    for e in errors:
        print(e)
    n_ep = len(cfg.get('endpoints') or [])
    n_ext = len(cfg.get('external-endpoints') or [])
    print(f'{Path(args.config).name}: {n_ep} endpoints, {n_ext} external endpoints, '
          f'{len(errors)} problem(s)')
    return 1 if errors else 0


if __name__ == '__main__':
    raise SystemExit(main())

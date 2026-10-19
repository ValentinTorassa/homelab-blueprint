#!/usr/bin/env python3
"""Backup coherence: do all the places that describe the backups agree?

What gets backed up, when, for how long and where it goes offsite is written
down in several places that nobody compares by hand:

  - the cron lines that actually run the snapshots (time, KEEP, family);
  - the offsite job's DAILY/WEEKLY lists;
  - the families the weekly restore test knows how to verify;
  - the documented disaster-recovery inventory (inventory.json), including the
    list of deployed apps that hold state.

When one changes and the others do not, the documentation promises a backup
that does not exist, or a backup exists that nothing ever restores. This check
fails when:

  - a family appears in one source and not in another;
  - the inventory gives a different time, KEEP or offsite cadence than the
    cron line and the offsite lists;
  - a deployed app is covered by no family, unless it is a KNOWN GAP (with a
    written reason) or its state lives in a family that does exist;
  - a known gap has been closed: the entry is now stale and must go, so the
    list of excuses cannot quietly outlive the excuse;
  - a dated status note in the docs ("last verified 2026-01-31") is older than
    --max-age-days: a claim of verification expires.

    check_backup_consistency.py [--cron FILE] [--offsite FILE] [--inventory FILE]
                                [--docs DIR] [--max-age-days 30]

Exit 0 coherent, 1 problems (one line each), 2 a source is missing.
Standard library only.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXAMPLES = HERE / 'examples'

FAMILY_RE = re.compile(r'backup_snapshot\.sh\s+([a-z0-9][a-z0-9-]*)')
KEEP_RE = re.compile(r'\bKEEP=(\d+)\b')
STATUS_RE = re.compile(r'(?i)(last verified|verified on|live[- ]checked|as of)\b.{0,60}?(20\d\d-\d\d-\d\d)')
HISTORICAL_RE = re.compile(r'(^|/)(runs|history)(/|$)|history')


def cron_families(path: Path) -> dict[str, dict]:
    """{family: {'time': 'HH:MM', 'keep': int|None, 'where': 'file:line'}}."""
    out = {}
    for n, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
        fields = line.split()
        if not fields or line.lstrip().startswith('#') or len(fields) < 7:
            continue
        if not (fields[0].isdigit() and fields[1].isdigit()):
            continue
        for fam in FAMILY_RE.findall(line):
            keep = KEEP_RE.search(line)
            out[fam] = {'time': f'{int(fields[1]):02d}:{int(fields[0]):02d}',
                        'keep': int(keep.group(1)) if keep else None,
                        'where': f'{path.name}:{n}'}
    return out


def bash_array(text: str, name: str) -> list[str]:
    m = re.search(rf'^{name}=\((.*?)\)', text, re.S | re.M)
    return m.group(1).split() if m else []


def offsite_lists(path: Path) -> dict[str, str]:
    """{family: 'daily'|'weekly'} from the offsite job's arrays."""
    text = path.read_text(encoding='utf-8')
    out = {f: 'weekly' for f in bash_array(text, 'WEEKLY')}
    out.update({f: 'daily' for f in bash_array(text, 'DAILY')})
    return out


def restore_test_families() -> set[str]:
    sys.path.insert(0, str(HERE))
    import restore_test  # noqa: PLC0415  (local module, imported on demand)
    return set(restore_test.FAMILIES)


def check(cron: dict, offsite: dict, restore: set, inventory: dict) -> list[str]:
    errors = []
    documented = inventory.get('families', {})
    sources = {
        'the cron lines': set(cron),
        'the restore test': restore,
        'inventory.json': set(documented),
        'the offsite lists': set(offsite) | {f for f, row in documented.items()
                                             if row.get('offsite') == 'none'},
    }
    everything = set().union(*sources.values())
    for fam in sorted(everything):
        absent = [name for name, fams in sources.items() if fam not in fams]
        if absent:
            errors.append(f'{fam}: missing from {", ".join(absent)}')

    for fam, row in sorted(documented.items()):
        c = cron.get(fam)
        if c:
            if row.get('time') != c['time']:
                errors.append(f'{fam}: inventory says {row.get("time")}, {c["where"]} runs at {c["time"]}')
            if c['keep'] is None:
                errors.append(f'{fam}: {c["where"]} does not pin KEEP= (inventory says {row.get("keep")})')
            elif row.get('keep') != c['keep']:
                errors.append(f'{fam}: inventory says KEEP {row.get("keep")}, {c["where"]} keeps {c["keep"]}')
        wanted = row.get('offsite')
        actual = offsite.get(fam, 'none')
        if wanted != actual:
            errors.append(f'{fam}: inventory says offsite {wanted}, the offsite lists say {actual}')

    covered = set(documented)
    gaps = inventory.get('known_gaps', {})
    elsewhere = inventory.get('state_elsewhere', {})
    for app in inventory.get('apps', []):
        if app in gaps:
            if app in covered:
                errors.append(f'{app}: listed as a known gap but family {app!r} exists; '
                              f'remove the stale gap')
            elif not str(gaps[app]).strip():
                errors.append(f'{app}: known gap without a reason')
            continue
        if app in elsewhere:
            if elsewhere[app] not in covered:
                errors.append(f'{app}: its state lives in {elsewhere[app]!r}, which is not backed up')
            continue
        if app not in covered:
            errors.append(f'{app}: deployed app with state but no backup family, gap or reason')
    for gap in gaps:
        if gap not in inventory.get('apps', []):
            errors.append(f'{gap}: known gap for an app that is no longer deployed; remove it')
    return errors


def stale_notes(docs: Path, max_age_days: int, today: dt.date | None = None) -> list[str]:
    today = today or dt.date.today()
    errors = []
    for path in sorted(docs.rglob('*.md')):
        rel = path.relative_to(docs).as_posix()
        if HISTORICAL_RE.search(rel):
            continue    # a run log or a history file is SUPPOSED to hold old dates
        for n, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
            for m in STATUS_RE.finditer(line):
                try:
                    when = dt.date.fromisoformat(m.group(2))
                except ValueError:
                    continue
                age = (today - when).days
                if age > max_age_days:
                    errors.append(f'{rel}:{n}: "{m.group(1)} {m.group(2)}" is {age} days old '
                                  f'(max {max_age_days}): re-verify or reword')
    return errors


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--cron', type=Path, default=EXAMPLES / 'cron.d-backups')
    ap.add_argument('--offsite', type=Path, default=EXAMPLES / 'offsite.conf')
    ap.add_argument('--inventory', type=Path, default=EXAMPLES / 'inventory.json')
    ap.add_argument('--docs', type=Path, help='directory of Markdown docs to scan for dated status notes')
    ap.add_argument('--max-age-days', type=int, default=30)
    args = ap.parse_args(argv)
    try:
        cron = cron_families(args.cron)
        offsite = offsite_lists(args.offsite)
        inventory = json.loads(args.inventory.read_text(encoding='utf-8'))
        restore = restore_test_families()
    except (OSError, ValueError) as exc:
        print(f'cannot read a source: {exc}', file=sys.stderr)
        return 2
    errors = check(cron, offsite, restore, inventory)
    if args.docs:
        errors += stale_notes(args.docs, args.max_age_days)
    for e in errors:
        print(e)
    for app, reason in sorted(inventory.get('known_gaps', {}).items()):
        print(f'known gap: {app}: {reason}')
    print(f'{len(inventory.get("families", {}))} families, {len(errors)} problem(s)')
    return 1 if errors else 0


if __name__ == '__main__':
    raise SystemExit(main())

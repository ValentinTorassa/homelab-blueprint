# Backups you can restore

A backup that has never been restored is a hypothesis. This folder covers the
whole chain: from the nightly snapshot to proof, every week, that the
snapshot is usable.

```text
02:30-02:40  backup_snapshot.sh <family>     nightly, one family per cron line   → Gatus Jobs/backup-<family>  (30h)
03:45        offsite push to S3              daily or weekly per family          → Gatus Jobs/backup-offsite-s3 (30h)
Wed 05:00    restore_test.py                 restores every family into scratch  → Gatus Jobs/restore-test    (192h, threshold 1)
CI, on push  check_backup_consistency.py     do all the descriptions agree?      → CI red
```

## 1. Nightly snapshots: [`backup_snapshot.sh`](backup_snapshot.sh)

Each family is configured by a small env file: the source tree, its SQLite
databases, an optional dump command (such as `pg_dump` via `docker exec`),
excludes and `KEEP`. Each run produces a self-describing directory:

```text
/srv/backups/app-a/
  2026-01-05_02-30-00/        the tree, a consistent copy of each DB, dump.sql, SHA256SUMS
  2026-01-06_02-30-00/
  latest -> 2026-01-06_02-30-00
```

- **Consistent database copies.** SQLite goes through the online backup API,
  never `cp` on a live file, followed by a `quick_check` on the copy.
- **Build hidden, verify, publish atomically, rotate last.** The snapshot is
  built in `.partial-*`. It must be non-empty, every database copy must pass
  its check, and `SHA256SUMS` is written outside the snapshot first, so it
  never lists itself half-written. Only then is it published with
  `mv -T` plus an atomic symlink swap, and only after that are old snapshots
  rotated out. A failing run never shrinks retention and never leaves
  `latest` pointing at half a snapshot. The tests pin this behaviour.
- **Private by default.** `umask 077` and a 0700 family directory, because
  snapshots carry `.env` files and keys.
- **Refuses an unmounted target**, rather than quietly filling the root
  filesystem.
- `KEEP=` on the cron line wins over the config file. That way the coherence
  check can compare it with the documented retention.

## 2. Offsite (described, not included)

A daily job pushes the small, fast-changing families to S3 (RPO ≤ 24 h). The
large, mostly-media ones go weekly (RPO ≤ 7 days). Before shipping, it refuses
any `latest` that is missing, older than 36 h, or **empty**. A freshness-only
check once passed empty directories every night with a green tick. Secrets
(`.env` files and tunnel credentials) travel as a single gpg-encrypted
bundle. Its passphrase lives in a password manager that does not depend on
the homelab.

The bucket is versioned and has S3 Object Lock enabled.
The uploader can put objects and read them back, but it **cannot delete
them** or loosen retention: see [`../standby/iam/`](../standby/iam/). A
compromised server can add new versions but cannot destroy the old ones.

## 3. Weekly restore test: [`restore_test.py`](restore_test.py)

For every family, it copies `latest` into a throwaway scratch directory and
asserts something that is true only if the backup is usable:

| Family | What is proven |
|---|---|
| `app-a` | `SHA256SUMS` matches. The SQLite copy passes `integrity_check` and has rows. Every `attachments` row has its file with the size the DB records. The `.env` is present (checked by stat only) |
| `blog` | `SHA256SUMS` matches, and the posts DB passes `integrity_check` with at least one post |
| `mailer` | `dump.sql` loads with **zero** errors (`ON_ERROR_STOP`) into a brand-new Postgres in a container with **no network**, and `subscribers` has rows |

The safety rules are enforced in code and pinned by tests:

- **Backups are read, never written.** Everything is copied to scratch first,
  and only the copy is opened. Not even SQLite is opened in place, because
  that can create `-journal` files next to it. The systemd unit adds
  `ReadOnlyPaths=/srv` on top.
- **Other systems' credentials are only `stat()`-ed.** The test checks they
  exist and are non-empty. It never reads or copies them.
- **Throwaway containers are fenced.** Their names start with
  `restoretest-`. They run with `--network none` and `--pull never`, no
  published ports, resource limits, and read-only mounts from scratch only.
  Cleanup never touches a container that lacks both the label and the name
  prefix.
- **Disk caps.** A family that would not fit, or would leave too little free
  space, comes out `unknown` instead of risking the host.
- **`unknown` is red.** "Could not test" is not a pass.
- **Isolation.** Each family runs in its own process under a timeout, and its
  scratch is deleted before the next family starts. A unit-level
  `ExecStopPost` cleans up after a run that got killed.
- **No user data in results.** The status and history files hold counts,
  sizes and hashes, never file names or content.
- The last stderr line summarizes everything that is not green. That is the
  text the Gatus alert carries.

In production the first full run passed every family (13 of 13). Families
added since then had to join the test in the same change, because the
coherence check below fails otherwise.

## 4. Coherence: [`check_backup_consistency.py`](check_backup_consistency.py)

What is backed up, when, for how long and where it goes offsite is written
down in four places: the cron lines, the offsite lists, the restore test, and
the DR inventory ([`examples/inventory.json`](examples/inventory.json)). If
one changes without the others, the docs promise a backup that does not
exist, or a backup exists that nothing ever restores. The check fails when:

- a family appears in one source but not in another;
- the inventory's time, `KEEP` or offsite cadence differs from the cron line
  or the offsite lists;
- a deployed app with state has no family, unless it is a **known gap** with
  a written reason, or its state lives in another family that *is* covered;
- a known gap has been closed. The entry is now stale and must be removed, so
  the list of excuses cannot outlive the excuse;
- a dated claim in the docs ("last verified 2026-01-31") is older than 30
  days, because claims of verification expire.

```bash
python3 backups/check_backup_consistency.py            # the examples: coherent
python3 backups/check_backup_consistency.py --docs docs --max-age-days 30
```

In the real setup the same check runs in CI on every push and on a daily
schedule, since documentation goes stale without any commit.

## Demo

```bash
bash backups/demo.sh
```

It snapshots two sample apps, restores them (green), then appends one byte to
a file inside the newest backup and restores again (red, with the reason).
No root and no Docker are needed.

#!/usr/bin/env bash
#
# backup_snapshot.sh: one nightly, self-describing snapshot of one backup family.
#
#   backup_snapshot.sh <family> [BACKUP_ROOT]        # default BACKUP_ROOT=/srv/backups
#
# Reads $BACKUP_CONF_DIR/<family>.env (default /etc/homelab/backup.d):
#
#   SOURCE_DIR=/srv/apps/app-a     # tree to snapshot (code, config, uploads, .env)
#   SQLITE_DBS="data/app.db"       # relative to SOURCE_DIR; copied with SQLite's
#                                  # online backup API instead of as raw files
#   DUMP_CMD=""                    # optional; its stdout is saved as dump.sql
#                                  # (e.g. "docker exec mailer-db pg_dump -U mailer mailer")
#   EXCLUDE="node_modules cache"   # optional; paths relative to SOURCE_DIR
#   KEEP=7                         # snapshots to keep; KEEP= from the cron line wins
#
# Produces BACKUP_ROOT/<family>/YYYY-MM-DD_HH-MM-SS/ with the tree, a consistent
# copy of each database, an optional dump.sql and a SHA256SUMS manifest, then
# points BACKUP_ROOT/<family>/latest at it.
#
# Order matters:
#   1. build into a hidden .partial-* directory;
#   2. verify it (non-empty, each SQLite copy passes quick_check, checksums);
#   3. publish with an atomic rename and an atomic symlink swap;
#   4. only THEN rotate old snapshots.
# A failing run therefore never shrinks retention and never leaves `latest`
# pointing at half a snapshot.
#
# Wrap it with run-and-report.sh so the result reaches Gatus; the last stderr
# line is the error message that shows up in the alert.

set -euo pipefail

FAMILY="${1:-}"
BACKUP_ROOT="${2:-${BACKUP_ROOT:-/srv/backups}}"
CONF_DIR="${BACKUP_CONF_DIR:-/etc/homelab/backup.d}"

die() { echo "backup ${FAMILY:-?}: $*" >&2; exit 1; }
say() { printf '[%s] %s\n' "$(date '+%F %T')" "$*"; }

[[ "$FAMILY" =~ ^[a-z0-9][a-z0-9-]*$ ]] || die "usage: backup_snapshot.sh <family> [BACKUP_ROOT]"
CONF="$CONF_DIR/$FAMILY.env"
[[ -r "$CONF" ]] || die "no config at $CONF"

keep_from_env="${KEEP:-}"
SOURCE_DIR="" SQLITE_DBS="" DUMP_CMD="" EXCLUDE="" KEEP=""
# shellcheck disable=SC1090
source "$CONF"
KEEP="${keep_from_env:-${KEEP:-7}}"
[[ "$KEEP" =~ ^[1-9][0-9]*$ ]] || die "KEEP must be a positive integer, got '$KEEP'"

# --- guards --------------------------------------------------------------------
if [[ "${REQUIRE_MOUNT:-1}" != 0 ]] && ! mountpoint -q "$BACKUP_ROOT"; then
  die "$BACKUP_ROOT is not mounted; refusing to fill the root filesystem"
fi
[[ -d "$SOURCE_DIR" ]] || die "SOURCE_DIR does not exist: $SOURCE_DIR"

# Snapshots carry .env files and keys: only the owner (root) can read them.
umask 077
BASE="$BACKUP_ROOT/$FAMILY"
mkdir -p "$BASE"
chmod 700 "$BASE"

exec 9>"$BASE/.lock"
flock -n 9 || die "another backup of $FAMILY is running"

DATE="$(date +%F_%H-%M-%S)"
STAGE="$BASE/.partial-$DATE"
SUMS_TMP="$BASE/.sums-$DATE"
cleanup() { rm -rf --one-file-system -- "$STAGE" "$SUMS_TMP" 2>/dev/null || true; }
trap cleanup EXIT
mkdir "$STAGE"

# --- 1. the tree, without the live database files ------------------------------
excludes=()
for rel in $EXCLUDE; do excludes+=("--exclude=./$rel"); done
for db in $SQLITE_DBS; do
  for suffix in "" -wal -shm -journal; do excludes+=("--exclude=./$db$suffix"); done
done
say "copying $SOURCE_DIR"
tar -C "$SOURCE_DIR" --anchored "${excludes[@]}" -cf - . | tar -C "$STAGE" -xf -

# --- 2. consistent database copies ---------------------------------------------
for db in $SQLITE_DBS; do
  [[ -f "$SOURCE_DIR/$db" ]] || die "SQLite database missing: $db"
  mkdir -p "$(dirname "$STAGE/$db")"
  say "sqlite online backup: $db"
  python3 - "$SOURCE_DIR/$db" "$STAGE/$db" <<'PY' || die "sqlite backup of $db failed"
import sqlite3, sys
src = sqlite3.connect(sys.argv[1])
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)                       # consistent even while the app writes
ok = dst.execute('PRAGMA quick_check').fetchone()[0]
dst.close()
src.close()
sys.exit(0 if ok == 'ok' else 1)
PY
done

if [[ -n "$DUMP_CMD" ]]; then
  say "dump: running DUMP_CMD"
  bash -o pipefail -c "$DUMP_CMD" > "$STAGE/dump.sql" || die "DUMP_CMD failed"
  [[ -s "$STAGE/dump.sql" ]] || die "DUMP_CMD produced an empty dump"
fi

# --- 3. verify and describe -----------------------------------------------------
nfiles="$(find "$STAGE" -type f | wc -l)"
(( nfiles > 0 )) || die "snapshot is empty"
# Written outside the snapshot first, so the manifest never lists itself half-written.
(cd "$STAGE" && find . -type f -print0 | sort -z | xargs -0 -r sha256sum) > "$SUMS_TMP"
mv "$SUMS_TMP" "$STAGE/SHA256SUMS"

# --- 4. publish atomically ------------------------------------------------------
mv -T "$STAGE" "$BASE/$DATE"
ln -sfn "$DATE" "$BASE/.latest.tmp"
mv -Tf "$BASE/.latest.tmp" "$BASE/latest"
size="$(du -sh "$BASE/$DATE" | cut -f1)"
say "published $FAMILY/$DATE ($nfiles files, $size)"

# --- 5. rotate, only now that a good snapshot exists ----------------------------
mapfile -t snaps < <(find "$BASE" -mindepth 1 -maxdepth 1 -type d \
  -regextype posix-extended -regex '.*/[0-9]{4}-[0-9]{2}-[0-9]{2}_[0-9]{2}-[0-9]{2}-[0-9]{2}' \
  -printf '%f\n' | sort)
if (( ${#snaps[@]} > KEEP )); then
  for old in "${snaps[@]:0:${#snaps[@]}-KEEP}"; do
    say "rotating out $FAMILY/$old"
    rm -rf --one-file-system -- "${BASE:?}/$old"
  done
fi
say "done: $FAMILY keeps $(( ${#snaps[@]} < KEEP ? ${#snaps[@]} : KEEP )) snapshot(s)"

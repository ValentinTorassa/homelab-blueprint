#!/usr/bin/env bash
#
# demo.sh: the backup pipeline end to end, in a temp directory, no root needed.
#
#   1. builds two sample apps (SQLite + uploads + a .env, and a blog database);
#   2. snapshots them with backup_snapshot.sh;
#   3. runs restore_test.py on the result: green;
#   4. silently corrupts one file inside the newest backup (bit rot, a bad disk,
#      a buggy job) and runs the restore test again: red, with the reason on the
#      last stderr line, which is what the Gatus alert would carry.
#
# The `mailer` family needs docker and a local postgres image, so it is left out.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/apps/app-a/data" "$WORK/apps/app-a/uploads" "$WORK/apps/blog" "$WORK/conf" "$WORK/backups"

python3 - "$WORK/apps" <<'PY'
import sqlite3, sys
from pathlib import Path
apps = Path(sys.argv[1])
db = sqlite3.connect(apps / 'app-a/data/app.db')
db.executescript('''
  CREATE TABLE items (id INTEGER PRIMARY KEY, title TEXT);
  CREATE TABLE attachments (id INTEGER PRIMARY KEY, item_id INTEGER, ext TEXT, size_bytes INTEGER);
''')
for i in range(1, 4):
    data = bytes([i]) * (1000 * i)
    (apps / f'app-a/uploads/{i}.png').write_bytes(data)
    db.execute('INSERT INTO items (title) VALUES (?)', (f'item {i}',))
    db.execute('INSERT INTO attachments (item_id, ext, size_bytes) VALUES (?, ?, ?)', (i, 'png', len(data)))
db.commit()
(apps / 'app-a/.env').write_text('API_TOKEN=not-a-real-token\n')
blog = sqlite3.connect(apps / 'blog/blog.db')
blog.execute('CREATE TABLE posts (id INTEGER PRIMARY KEY, slug TEXT, body TEXT)')
blog.execute("INSERT INTO posts (slug, body) VALUES ('hello', 'first post')")
blog.commit()
PY

printf 'SOURCE_DIR=%s\nSQLITE_DBS="data/app.db"\nKEEP=7\n' "$WORK/apps/app-a" > "$WORK/conf/app-a.env"
printf 'SOURCE_DIR=%s\nSQLITE_DBS="blog.db"\nKEEP=7\n' "$WORK/apps/blog" > "$WORK/conf/blog.env"

echo "== 1. nightly snapshots"
for fam in app-a blog; do
  REQUIRE_MOUNT=0 BACKUP_CONF_DIR="$WORK/conf" "$HERE/backup_snapshot.sh" "$fam" "$WORK/backups"
done

rt() {
  python3 "$HERE/restore_test.py" --backup-root "$WORK/backups" --state-dir "$WORK/state" \
    --family app-a --family blog --min-free-gb 0 --docker false
}

echo; echo "== 2. weekly restore test on good backups"
rt

echo; echo "== 3. one upload inside the newest backup rots"
printf 'x' >> "$WORK/backups/app-a/latest/uploads/2.png"
set +e
rt
rc=$?
set -e
echo; echo "restore test exit code: $rc (non-zero: run-and-report.sh would push a failure to Gatus)"

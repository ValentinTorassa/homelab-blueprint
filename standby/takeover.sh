#!/usr/bin/env bash
#
# takeover.sh: bring the services back on the cold standby. Runs ON the standby, as root.
#
#   sudo ./takeover.sh --dry-run                                   # pull, refresh, check; start nothing
#   sudo ./takeover.sh --pass-file /root/offsite.pass --primary-is-down
#
# Why it refuses a real run without --primary-is-down: the standby carries the
# same tunnel credentials and the same API keys as the primary. With both
# alive, the tunnel would split live traffic between two hosts, a bot would
# answer every message twice, and a scheduler would publish the same posts
# twice. Only a person can confirm the primary is really down and will stay
# down, so the flag is that person's signature.
#
# The passphrase decrypts the secrets bundle. It lives in a password manager
# that does NOT depend on the homelab (no circular dependency). Write it to a
# root-only file, run this, delete the file.
#
# Steps:
#   1. Pull the newest bundle of every family from S3 (same region: no egress).
#   2. Decrypt the secrets bundle and put back each service's .env and the
#      tunnel credentials. Skipped on --dry-run: a rehearsal host holds no
#      live secret.
#   3. Refresh each service's data from its bundle.
#   4. HOLD every scheduled job that is already due ("held-failover"). A
#      scheduler started from a snapshot would otherwise publish everything
#      that was pending when the snapshot was taken, including what the primary
#      already published since. Held jobs are released by a person, one by one.
#      On --dry-run, every live API key is also stripped from the job store.
#   5. Remove the rehearsal block from /etc/hosts, start the services and the
#      tunnel, and print what answers.
set -euo pipefail

BUCKET="${BUCKET:-example-offsite-bucket}"
REGION="${REGION:-us-east-1}"
APPS=/srv/apps                       # where each service lives on the standby
STAGE=/srv/takeover
SERVICE_USER=svc
FAMILIES=(app-a app-b)               # data bundles the services need
SERVICES=(app-a app-b scheduler)     # systemd units, installed but disabled
JOB_STORES=("$APPS/app-a/data/scheduled-jobs.json")
HEALTH_URLS=(http://127.0.0.1:3001/api/health http://127.0.0.1:3002/healthz)

PASS_FILE=""; DOWN=0; DRY=0
while (( $# )); do
  case "$1" in
    --pass-file) PASS_FILE="${2:-}"; shift 2 ;;
    --primary-is-down) DOWN=1; shift ;;
    --dry-run) DRY=1; shift ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[[ "$(id -u)" == 0 ]] || { echo "run as root" >&2; exit 2; }
if (( ! DRY )); then
  (( DOWN )) || { echo "refusing: pass --primary-is-down once you have confirmed the primary is really down" >&2; exit 2; }
  [[ -s "$PASS_FILE" ]] || { echo "refusing: --pass-file with the offsite passphrase is required" >&2; exit 2; }
fi
log() { printf '[%s] %s\n' "$(date +%T)" "$*"; }
t0=$SECONDS
umask 077

log "1/5 pulling the newest bundles from s3://$BUCKET/bundles/"
newest() {
  aws s3 ls "s3://$BUCKET/bundles/$1/" --region "$REGION" | awk '{print $4}' | grep -v '^$' | sort | tail -1
}
declare -A DIR
mkdir -p "$STAGE"
for fam in "${FAMILIES[@]}"; do
  obj="$(newest "$fam")"
  [[ -n "$obj" ]] || { log "  no bundle for $fam"; exit 1; }
  rm -rf "${STAGE:?}/$fam"; mkdir -p "$STAGE/$fam"
  aws s3 cp "s3://$BUCKET/bundles/$fam/$obj" "$STAGE/$fam/" --region "$REGION" --only-show-errors
  tar -xf "$STAGE/$fam/$obj" -C "$STAGE/$fam" && rm -f "$STAGE/$fam/$obj"
  DIR[$fam]="$(find "$STAGE/$fam" -mindepth 1 -maxdepth 1 -type d | sort | tail -1)"
  log "  $fam <- $obj"
done

log "2/5 secrets"
if (( DRY )); then
  log "  dry run: not decrypting anything"
else
  sec="$(newest secrets)"
  aws s3 cp "s3://$BUCKET/bundles/secrets/$sec" "$STAGE/secrets.tar.gz.gpg" --region "$REGION" --only-show-errors
  gpg --batch --yes --passphrase-file "$PASS_FILE" -d "$STAGE/secrets.tar.gz.gpg" | tar -xz -C "$STAGE"
  rm -f "$STAGE/secrets.tar.gz.gpg"
  (cd "$STAGE/secrets/env" && find . -type f) | while read -r f; do
    install -D -o "$SERVICE_USER" -g "$SERVICE_USER" -m 600 "$STAGE/secrets/env/$f" "$APPS/${f#./}"
    log "  restored ${f#./}"
  done
  svc_home="$(getent passwd "$SERVICE_USER" | cut -d: -f6)"
  install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 700 "$svc_home/.cloudflared"
  install -o "$SERVICE_USER" -g "$SERVICE_USER" -m 600 "$STAGE"/secrets/tunnel/*.json "$svc_home/.cloudflared/"
fi

log "3/5 data"
for fam in "${FAMILIES[@]}"; do
  rsync -a --delete "${DIR[$fam]}/data/" "$APPS/$fam/data/"
  log "  $fam data refreshed ($(find "$APPS/$fam/data" -type f | wc -l) files)"
done
chown -R "$SERVICE_USER:$SERVICE_USER" "$APPS"

log "4/5 holding scheduled jobs that are already due"
for store in "${JOB_STORES[@]}"; do
  DRY=$DRY python3 - "$store" <<'PY'
import datetime as dt, json, os, sys
path = sys.argv[1]
doc = json.load(open(path))
jobs = doc['jobs'] if isinstance(doc, dict) else doc
now = dt.datetime.now(dt.timezone.utc)
held = 0
for job in jobs:
    if job.get('status') in ('pending', 'running'):
        at = dt.datetime.fromisoformat(str(job.get('scheduledAt', '')).replace('Z', '+00:00'))
        if at <= now:
            job['status'], job['heldAt'] = 'held-failover', now.isoformat()
            held += 1
    if os.environ.get('DRY') == '1':
        job.pop('apiKey', None)       # a rehearsal host never carries a live key
json.dump(doc, open(path, 'w'), indent=2)
pending = sum(1 for j in jobs if j.get('status') == 'pending')
print(f'  {os.path.basename(path)}: {held} due job(s) held, {pending} still pending')
PY
done

if (( DRY )); then
  log "dry run done in $(( SECONDS - t0 ))s: nothing started, rehearsal block kept"
  exit 0
fi

log "5/5 starting"
sed -i '/^# BEGIN REHEARSAL-BLOCK/,/^# END REHEARSAL-BLOCK/d' /etc/hosts
systemctl daemon-reload
systemctl enable --now "${SERVICES[@]}" cloudflared
sleep 8
for url in "${HEALTH_URLS[@]}"; do
  printf '  %-40s %s\n' "$url" "$(curl -s -o /dev/null -m 5 -w '%{http_code}' "$url")"
done
for unit in "${SERVICES[@]}" cloudflared; do
  printf '  %-20s %s\n' "$unit" "$(systemctl is-active "$unit")"
done
log "takeover finished in $(( SECONDS - t0 ))s. Review the held-failover jobs before releasing any."

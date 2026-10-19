#!/usr/bin/env bash
#
# report-job.sh: push the RESULT of a scheduled job to a Gatus external endpoint.
#
# Why
# ---
# Gatus already knows whether a process answers on its port. That does not
# tell you whether the job behind it did its work. The failures that actually
# hurt in this homelab were all of the second kind, with the dashboard green:
# a weekly ingest that silently never ran, an API token that expired while
# nobody looked, a publisher that logged a WARNING and carried on.
#
# A system that fails silently is indistinguishable from one that works.
#
# How
# ---
# Every job pushes its outcome to its own external endpoint. Two properties
# matter, and both are load-bearing:
#
#   1. Push ALWAYS, on success and on failure. A reporter that only speaks on
#      failure cannot tell "all good" from "never ran".
#   2. Every external endpoint declares `heartbeat.interval`. If the job does
#      not run at all (cron has no catch-up; a box that is down at 03:00 loses
#      that run), nothing is pushed and the expired heartbeat IS the alert.
#
# Usage
# -----
#   report-job.sh <key> <ok|fail> [error message] [duration in seconds]
#
#   report-job.sh backup-app-a ok "" 42
#   report-job.sh api-token fail "token expires in 3 days"
#
# Normally you do not call this directly: run-and-report.sh wraps a command
# and calls it for you.
#
# Config
# ------
# $JOBS_ENV_FILE (default ~/.config/homelab/gatus-jobs.env), mode 0600, never
# in git:
#
#   GATUS_BASE_URL=https://status.home.example.internal
#   GATUS_TOKEN_BACKUP_APP_A=<openssl rand -hex 32>
#
# One token per endpoint: a leaked token can only fake that one job. The
# variable name is the key upper-cased with dashes turned into underscores.

set -uo pipefail   # deliberately no -e: this script must never kill the job

readonly JOBS_ENV_FILE="${JOBS_ENV_FILE:-$HOME/.config/homelab/gatus-jobs.env}"
readonly LOG_FILE="${JOBS_LOG_FILE:-$HOME/.local/state/homelab/job-reports.log}"
readonly CURL="${CURL_BIN:-curl}"

# Gatus addresses an endpoint as "<group>_<name>", both lower-cased with
# spaces turned into dashes. The key passed to this script is only the name,
# so the group is prepended here. Get it wrong and Gatus answers 404, which a
# naive script reports as "Gatus unreachable" while the endpoint waits forever.
readonly GATUS_GROUP="${GATUS_GROUP:-jobs}"

log() {
    mkdir -p "$(dirname "$LOG_FILE")" 2>/dev/null
    printf '[%s] report-job: %s\n' "$(date '+%Y-%m-%d %H:%M:%S %z')" "$*" >> "$LOG_FILE"
}

key="${1:-}"
state="${2:-}"
error_msg="${3:-}"
duration="${4:-0}"

if [[ -z "$key" || -z "$state" ]]; then
    echo "usage: report-job.sh <key> <ok|fail> [error] [seconds]" >&2
    exit 2
fi
if [[ ! "$key" =~ ^[a-z0-9][a-z0-9-]*$ ]]; then
    echo "invalid key: $key (lowercase letters, digits and dashes)" >&2
    exit 2
fi
case "$state" in
    ok|success|true)    success=true ;;
    fail|failure|false) success=false ;;
    *) echo "invalid state: $state (expected ok|fail)" >&2; exit 2 ;;
esac
[[ "$duration" =~ ^[0-9]+$ ]] || duration=0

if [[ ! -r "$JOBS_ENV_FILE" ]]; then
    log "MISSING $JOBS_ENV_FILE: could not report '$key' ($state)"
    exit 0
fi
# shellcheck disable=SC1090
source "$JOBS_ENV_FILE"

# key -> variable name: backup-app-a => GATUS_TOKEN_BACKUP_APP_A
token_var="GATUS_TOKEN_$(printf '%s' "$key" | tr 'a-z-' 'A-Z_')"
token="${!token_var:-}"

if [[ -z "${GATUS_BASE_URL:-}" || -z "$token" ]]; then
    log "no GATUS_BASE_URL or no $token_var: could not report '$key' ($state)"
    exit 0
fi

# Gatus stores the error with each result; a few hundred characters are enough
# to read what happened from a phone notification.
error_short="${error_msg:0:240}"
full_key="${GATUS_GROUP}_${key}"
url="${GATUS_BASE_URL%/}/api/v1/endpoints/${full_key}/external"

# -G puts the --data-urlencode pairs in the query string; -X POST keeps the
# method Gatus expects. No `|| echo 000`: curl already prints 000 when it
# cannot connect, and the echo would make it 000000.
code="$("$CURL" -s -o /dev/null -w '%{http_code}' --max-time 15 \
    -X POST -G \
    -H "Authorization: Bearer ${token}" \
    --data-urlencode "success=${success}" \
    --data-urlencode "error=${error_short}" \
    --data-urlencode "duration=${duration}s" \
    "$url" 2>/dev/null)"
code="${code:-000}"

if [[ "$code" == 2* ]]; then
    log "$key -> success=$success duration=${duration}s${error_short:+ error=\"$error_short\"}"
elif [[ "$code" == "404" ]]; then
    # A 404 is NOT "Gatus is down": the endpoint does not exist under that key.
    # Conflating the two leaves a job reporting into the void, which is the
    # exact bug this file exists to prevent.
    log "ERROR 404: Gatus does not know '$full_key' (missing external endpoint, or group is not '$GATUS_GROUP')"
elif [[ "$code" == "401" || "$code" == "403" ]]; then
    log "ERROR $code: token rejected for '$full_key'"
else
    # Being unable to report must not break the job. If the monitor or the
    # network is down, the job already did its work, and the expired heartbeat
    # will raise its hand once the monitor is back.
    log "COULD NOT PUSH $full_key (success=$success): Gatus unreachable (http=$code)"
fi

# Always 0, on purpose: this script is an observer, not a participant.
exit 0

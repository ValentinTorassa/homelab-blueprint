#!/usr/bin/env bash
#
# run-and-report.sh: run a command and report its outcome to Gatus.
#
#   run-and-report.sh <key> <command> [args...]
#
# Wrapping a job should not mean rewriting its cron line with an `if` and two
# `date` calls. A line goes from
#
#   10 6 * * * ops check_drift.py >> /var/log/homelab/drift.log 2>&1
#
# to
#
#   10 6 * * * ops run-and-report.sh config-drift check_drift.py >> /var/log/homelab/drift.log 2>&1
#
# The command keeps its stdout and stderr, so the usual log is written exactly
# as before. The only addition is the push.
#
# It exits with the SAME code as the wrapped command, so cron, systemd and the
# log still see the real failure and nothing else changes for anyone.
#
# Running as root (a backup from /etc/cron.d, a systemd service)? Set
# REPORT_AS=<user> and the reporter runs as that user through `sudo -u`, with
# that user's 0600 token file. The tokens stay owned by one unprivileged user,
# and root never executes a file that user could have edited.

set -uo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly SELF_DIR
readonly REPORTER="${REPORT_JOB_BIN:-$SELF_DIR/report-job.sh}"

key="${1:-}"
shift || true

if [[ -z "$key" || $# -eq 0 ]]; then
    echo "usage: run-and-report.sh <key> <command> [args...]" >&2
    exit 2
fi

start=$SECONDS

# stdout is not touched: it goes wherever it already went. Only stderr goes
# through a temp file, so its last lines can be sent as the error, and it is
# replayed to stderr as soon as the command ends.
#
# A temp file and not `2> >(tee ...)`: process substitution runs
# asynchronously, and `tail` can read the file before tee has flushed it, so
# the reported error comes out empty or truncated exactly when it is needed.
err_file="$(mktemp)"
trap 'rm -f "$err_file"' EXIT

"$@" 2>"$err_file"
rc=$?
cat "$err_file" >&2

duration=$(( SECONDS - start ))

report() {
    if [[ -n "${REPORT_AS:-}" && "$(id -un)" != "$REPORT_AS" ]]; then
        local home
        home="$(getent passwd "$REPORT_AS" | cut -d: -f6)"
        sudo -n -u "$REPORT_AS" HOME="$home" "$REPORTER" "$@"
    else
        "$REPORTER" "$@"
    fi
}

if [[ ! -x "$REPORTER" ]]; then
    echo "run-and-report: reporter $REPORTER not found; '$key' was not reported" >&2
elif [[ $rc -eq 0 ]]; then
    report "$key" ok "" "$duration" || true
else
    # The last lines of stderr are the useful ones: the traceback or the final
    # message. The first ones are usually start-up noise.
    tail_err="$(tail -c 400 "$err_file" 2>/dev/null | tr '\n' ' ' | tr -s ' ')"
    report "$key" fail "exit $rc: ${tail_err}" "$duration" || true
fi

exit $rc

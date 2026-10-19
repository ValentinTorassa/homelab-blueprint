#!/usr/bin/env bash
#
# takeover-drill.sh: rehearse the cold standby end to end, safely. Operator-run, never scheduled.
#
#   ./takeover-drill.sh --dry-run     # print every aws/ssh command, execute none
#   ./takeover-drill.sh               # the real drill
#
# In order:
#   1. Preflight, checks only: the AWS profile is the expected account, the
#      instance exists and is STOPPED. A running standby may be a real takeover
#      in progress, and this script's trap would stop it, so it refuses.
#   2. start-instances, retrying on InsufficientInstanceCapacity and then
#      falling back to other arm64 types (the first automated drill failed on
#      exactly that error).
#   3. Waits for "running", then for SSH over the VPN.
#   4. Runs exactly `sudo /root/takeover.sh --dry-run` on the standby. The
#      remote command is a constant: this script has no way to run a real
#      takeover and refuses any flag that looks like one.
#   5. ALWAYS stops the instance again (EXIT/INT/TERM trap), waits for
#      "stopped", and puts the resting instance type back.
#   6. Writes a timestamped evidence log and prints PASS/FAIL with timings.
#
# Exit: 0 PASS, 1 FAIL (standby confirmed stopped), 2 refused before touching
# anything, 3 FAIL AND the standby could not be confirmed stopped (it is
# costing money: stop it by hand).
set -uo pipefail

readonly REMOTE_CMD='sudo /root/takeover.sh --dry-run'

PROFILE="${AWS_PROFILE:-homelab}"
REGION="${AWS_REGION:-us-east-1}"
INSTANCE_ID="${STANDBY_INSTANCE_ID:-i-0123456789abcdef0}"
EXPECT_ACCOUNT="${EXPECT_ACCOUNT:-111122223333}"
SSH_TARGET="${STANDBY_SSH:-admin@standby.vpn.example.internal}"
FALLBACK_TYPES="${FALLBACK_TYPES:-m7g.medium t4g.large}"
START_ATTEMPTS=2 RETRY_DELAY=60 BOOT_TIMEOUT=600 SSH_TIMEOUT=300 TAKEOVER_TIMEOUT=1200 STOP_TIMEOUT=600
EVIDENCE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/homelab/drills"
DRY=0

refuse() { printf 'refused: %s\n' "$*" >&2; exit 2; }
while (( $# )); do
  case "$1" in
    --dry-run) DRY=1; shift ;;
    --primary-is-down*|--pass-file*|--real|--live|--remote-cmd*)
      refuse "'$1': this drill only ever runs '$REMOTE_CMD'. A real takeover is a manual step (runbook.md)." ;;
    -h|--help) sed -n '2,28p' "$0"; exit 0 ;;
    *) refuse "unknown argument: $1" ;;
  esac
done

AWS=(aws --profile "$PROFILE" --region "$REGION")
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
EVIDENCE=/dev/null
if (( ! DRY )); then
  umask 077; mkdir -p "$EVIDENCE_DIR"; EVIDENCE="$EVIDENCE_DIR/takeover-drill-$STAMP.log"
fi
log() { printf '[%s] %s\n' "$(date -u +%TZ)" "$*" | tee -a "$EVIDENCE"; }
now() { date +%s; }
pause() { (( DRY )) || sleep "$1"; }

SIM_STATE=stopped    # --dry-run walks the whole flow against a simulated instance
aws_do() {           # run (or, on --dry-run, print) one aws command
  if (( DRY )); then
    log "  \$ ${AWS[*]} $*" >&2      # stderr: visible even when the caller discards stdout
    case "$*" in *start-instances*) SIM_STATE=running ;; *stop-instances*) SIM_STATE=stopped ;; esac
    return 0
  fi
  "${AWS[@]}" "$@"
}
state() {
  if (( DRY )); then echo "$SIM_STATE"; return; fi
  "${AWS[@]}" ec2 describe-instances --instance-ids "$INSTANCE_ID" \
    --query 'Reservations[0].Instances[0].State.Name' --output text 2>/dev/null || echo unknown
}
wait_state() {       # wait_state <state> <timeout>
  local deadline=$(( $(now) + $2 ))
  until [[ "$(state)" == "$1" ]]; do (( $(now) < deadline )) || return 1; pause 5; done
}

RESULT=FAIL REASON="" STARTED=0 STOPPED=na ORIG_TYPE="" CUR_TYPE=""
T0=$(now) T_START="" T_RUNNING="" T_SSH="" T_READY=""
fail() { REASON="${REASON:-$*}"; log "FAIL: $*"; exit 1; }
dur() { [[ -n "$1" && -n "$2" ]] && echo "$(( $2 - $1 ))s" || echo n/a; }

# shellcheck disable=SC2329  # invoked from the EXIT trap
on_exit() {
  local rc=$?
  trap '' INT TERM            # a second Ctrl-C must not abort the stop
  if (( STARTED )); then
    log "stopping the standby"
    aws_do ec2 stop-instances --instance-ids "$INSTANCE_ID" >/dev/null
    if wait_state stopped "$STOP_TIMEOUT"; then STOPPED=yes; else STOPPED=NO; fi
    if [[ "$STOPPED" == yes && -n "$ORIG_TYPE" && "$CUR_TYPE" != "$ORIG_TYPE" ]]; then
      aws_do ec2 modify-instance-attribute --instance-id "$INSTANCE_ID" --instance-type "Value=$ORIG_TYPE" \
        && log "instance type restored to $ORIG_TYPE"
    fi
  fi
  (( rc == 2 && ! STARTED )) && return
  [[ $rc == 0 && -z "$REASON" && "$STOPPED" == yes ]] && RESULT=PASS
  {
    echo "==== takeover drill ===="
    echo "result:            $RESULT${REASON:+ ($REASON)}$( (( DRY )) && echo ' [dry run: simulated]')"
    echo "start -> running:  $(dur "$T_START" "$T_RUNNING")"
    echo "running -> ssh:    $(dur "$T_RUNNING" "$T_SSH")"
    echo "start -> ready:    $(dur "$T_START" "$T_READY")   (data staged; NOT an RTO: no traffic moved)"
    echo "stop confirmed:    $STOPPED"
    echo "total:             $(dur "$T0" "$(now)")"
  } | tee -a "$EVIDENCE"
  [[ "$STOPPED" == NO ]] && { log "STANDBY STILL RUNNING: ${AWS[*]} ec2 stop-instances --instance-ids $INSTANCE_ID"; exit 3; }
  [[ "$RESULT" == PASS ]] && exit 0
  exit 1
}
trap on_exit EXIT
trap 'REASON=${REASON:-interrupted}; exit 130' INT TERM

log "1/5 preflight (remote command, fixed: $REMOTE_CMD)"
if (( ! DRY )); then
  acct="$("${AWS[@]}" sts get-caller-identity --query Account --output text 2>&1)" || fail "profile $PROFILE does not authenticate"
  [[ "$acct" == "$EXPECT_ACCOUNT" ]] || fail "profile $PROFILE is account $acct, expected $EXPECT_ACCOUNT"
  ORIG_TYPE="$("${AWS[@]}" ec2 describe-instances --instance-ids "$INSTANCE_ID" \
    --query 'Reservations[0].Instances[0].InstanceType' --output text)" || fail "cannot describe $INSTANCE_ID"
else
  ORIG_TYPE=t4g.medium
fi
CUR_TYPE="$ORIG_TYPE"
st="$(state)"
[[ "$st" == stopped ]] || fail "standby is '$st', not 'stopped': maybe a real takeover; not touching it"

log "2/5 starting ($ORIG_TYPE, then: $FALLBACK_TYPES)"
STARTED=1; T_START=$(now)
started=0
for type in "$ORIG_TYPE" $FALLBACK_TYPES; do
  if [[ "$type" != "$CUR_TYPE" ]]; then
    aws_do ec2 modify-instance-attribute --instance-id "$INSTANCE_ID" --instance-type "Value=$type" || continue
    CUR_TYPE="$type"
  fi
  for (( try = 1; try <= START_ATTEMPTS; try++ )); do
    if (( DRY )); then aws_do ec2 start-instances --instance-ids "$INSTANCE_ID"; started=1; break 2; fi
    if out="$(aws_do ec2 start-instances --instance-ids "$INSTANCE_ID" 2>&1)"; then started=1; break 2; fi
    grep -q InsufficientInstanceCapacity <<<"$out" || fail "start-instances: $out"
    log "  no capacity for $type (attempt $try/$START_ATTEMPTS)"; pause "$RETRY_DELAY"
  done
done
(( started )) || fail "no capacity for any instance type"
wait_state running "$BOOT_TIMEOUT" || fail "not running after ${BOOT_TIMEOUT}s"
T_RUNNING=$(now); log "  running as $CUR_TYPE after $(dur "$T_START" "$T_RUNNING")"

log "3/5 waiting for SSH over the VPN"
SSH=(ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new "$SSH_TARGET")
if (( DRY )); then log "  \$ ${SSH[*]} true"; else
  deadline=$(( $(now) + SSH_TIMEOUT ))
  until "${SSH[@]}" true 2>/dev/null; do (( $(now) < deadline )) || fail "no SSH after ${SSH_TIMEOUT}s"; sleep 5; done
fi
T_SSH=$(now)

log "4/5 remote: $REMOTE_CMD"
if (( DRY )); then log "  \$ ${SSH[*]} $REMOTE_CMD"; else
  timeout "$TAKEOVER_TIMEOUT" "${SSH[@]}" "$REMOTE_CMD" 2>&1 | tee -a "$EVIDENCE"
  rc=${PIPESTATUS[0]}
  (( rc == 0 )) || fail "takeover --dry-run exited $rc"
fi
T_READY=$(now)
log "5/5 done; the trap stops the standby"

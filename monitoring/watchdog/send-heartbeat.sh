#!/usr/bin/env bash
#
# send-heartbeat.sh: the monitor proves it is alive to something outside the house.
#
# Gatus watches the server. Nothing on the home network can report that the
# monitor itself, the home internet or the power is gone: the thing that would
# alert is the thing that died. So the monitor sends a signed heartbeat every
# few minutes to the external alert relay, and a scheduled trigger on the relay
# side checks the age of the last one on its own clock (dead man's switch):
#
#   beats stop      -> one "warning" message
#   still missing   -> one "critical" message
#   back again      -> exactly one "recovery" message
#
# The relay stores the new alert state only after the chat webhook accepts the
# message, so a failed delivery is retried on the next tick instead of lost.
# The heartbeat route accepts only this HMAC signature, never the static bearer
# token Gatus uses, so a leaked Gatus token cannot keep a dead monitor "alive".
#
# Signature: X-Signature: t=<unix>,v1=hex(HMAC-SHA256(secret, "<t>.<sha256(body)>"))
# The relay rejects timestamps outside a short window (replay protection).
#
# Runs from a systemd timer as root; reads RELAY_URL and RELAY_HMAC_SECRET from
# a root-only env file.
set -euo pipefail

ENV_FILE="${HEARTBEAT_ENV_FILE:-/etc/homelab/alerts.env}"
# shellcheck disable=SC1090
source "$ENV_FILE"
: "${RELAY_URL:?missing RELAY_URL}"
: "${RELAY_HMAC_SECRET:?missing RELAY_HMAC_SECRET}"

body='{}'
ts="$(date +%s)"
body_hash="$(printf '%s' "$body" | sha256sum | cut -d ' ' -f 1)"
sig="$(printf '%s' "${ts}.${body_hash}" | openssl dgst -sha256 -hmac "$RELAY_HMAC_SECRET" | awk '{print $NF}')"

curl -fsS --retry 1 --connect-timeout 5 --max-time 15 \
  -X POST -H 'Content-Type: application/json' \
  -H "X-Signature: t=${ts},v1=${sig}" \
  --data-binary "$body" "${RELAY_URL%/}/heartbeat"

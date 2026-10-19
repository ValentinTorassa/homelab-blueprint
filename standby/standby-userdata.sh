#!/bin/bash
#
# Cloud-init user-data for the cold standby (see provision-standby.sh).
# Installs the runtimes, pre-stages the newest backup bundles from S3, and arms
# the REHEARSAL BLOCK: until takeover.sh removes it, the external APIs the
# services talk to resolve to nowhere, so a rehearsal can never post, send or
# charge anything for real.
exec > /var/log/standby-bootstrap.log 2>&1
set -euxo pipefail
export DEBIAN_FRONTEND=noninteractive
BUCKET=example-offsite-bucket
REGION=us-east-1

apt-get update
apt-get install -y ca-certificates curl gnupg unzip jq rsync sqlite3 docker.io
systemctl enable --now docker

# AWS CLI v2 (arm64). Credentials come from the instance role, never a key.
curl -fsSL https://awscli.amazonaws.com/awscli-exe-linux-aarch64.zip -o /tmp/awscli.zip
(cd /tmp && unzip -q awscli.zip && ./aws/install)

# VPN client: installed, NOT authenticated. Joining needs an auth key from the
# operator at takeover or drill time.
curl -fsSL https://tailscale.com/install.sh | sh
systemctl enable tailscaled

# Tunnel client: the same tunnel as the primary, so its credentials only arrive
# at takeover (secrets bundle). The service units are staged by the operator,
# installed and DISABLED; takeover.sh is the only thing that enables them.
curl -fsSL https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64 \
  -o /usr/local/bin/cloudflared
chmod +x /usr/local/bin/cloudflared

cat >> /etc/hosts <<'EOF'
# BEGIN REHEARSAL-BLOCK (removed by takeover.sh on a real takeover only)
0.0.0.0 api.publisher.example
0.0.0.0 api.chat.example
0.0.0.0 smtp.mail.example
# END REHEARSAL-BLOCK
EOF

# Pre-stage the bundles: same region, so this is fast and has no egress cost.
mkdir -p /srv/restore
aws s3 sync "s3://$BUCKET/bundles/" /srv/restore/ --region "$REGION" --exclude 'secrets/*'
du -sh /srv/restore/* || true
echo "BOOTSTRAP-OK $(date -Is)"

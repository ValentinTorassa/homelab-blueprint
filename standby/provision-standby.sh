#!/usr/bin/env bash
#
# provision-standby.sh: rebuild the cold standby on AWS from nothing.
#
#   AWS_PROFILE=homelab BUCKET=example-offsite-bucket ./provision-standby.sh
#
# Creates a security group, a read-only IAM role, a key pair and one small arm64
# instance whose user-data installs the runtimes and pre-stages the newest
# backups from S3. It fails loudly on names that already exist instead of
# silently reusing them.
#
# What it does NOT do, on purpose: authenticate the VPN, configure the tunnel,
# or copy any production credential. Those move live secrets onto a cloud host
# and are operator decisions, taken at takeover time (see runbook.md).
#
# After the bootstrap finishes (~6 min), STOP the instance. Stopped, it costs
# only its EBS volume; the auto-assigned public IPv4 is released on stop.
set -euo pipefail

: "${AWS_PROFILE:=homelab}"; export AWS_PROFILE
export AWS_REGION="${AWS_REGION:-us-east-1}"
BUCKET="${BUCKET:-example-offsite-bucket}"
NAME="${NAME:-homelab-standby}"
TYPE="${TYPE:-t4g.medium}"
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

# Debian publishes its official AMI ids as public SSM parameters: no account id
# to hard-code, and always the newest point release.
AMI=$(aws ssm get-parameter --name /aws/service/debian/release/bookworm/latest/arm64 \
  --query Parameter.Value --output text)
VPC=$(aws ec2 describe-vpcs --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId' --output text)
SUBNET=$(aws ec2 describe-subnets --filters "Name=vpc-id,Values=$VPC" --query 'Subnets[0].SubnetId' --output text)
MY_IP=$(curl -fsS https://checkip.amazonaws.com | tr -d '\n')
echo "ami=$AMI vpc=$VPC subnet=$SUBNET operator-ip=$MY_IP"

# SSH from the operator's current address only. Everything else (and, once the
# VPN is up, SSH too) goes over the VPN; no service port is ever opened.
SG=$(aws ec2 create-security-group --group-name "$NAME" \
  --description "Cold standby: SSH from the operator IP only, everything else over the VPN" \
  --vpc-id "$VPC" --query GroupId --output text)
aws ec2 authorize-security-group-ingress --group-id "$SG" --protocol tcp --port 22 \
  --cidr "${MY_IP}/32" >/dev/null

# Read-only role: the standby restores FROM the bucket and can never modify it.
# No access key ever lands on the machine; it gets short-lived role credentials.
aws iam create-role --role-name "$NAME-role" --assume-role-policy-document \
  '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}' >/dev/null
aws iam put-role-policy --role-name "$NAME-role" --policy-name "$NAME-read-backups" \
  --policy-document "$(sed "s/example-offsite-bucket/$BUCKET/g" "$HERE/iam/standby-read-only.json")"
aws iam create-instance-profile --instance-profile-name "$NAME-profile" >/dev/null
aws iam add-role-to-instance-profile --instance-profile-name "$NAME-profile" --role-name "$NAME-role"
sleep 10   # a fresh instance profile is not immediately usable by RunInstances

umask 077
aws ec2 create-key-pair --key-name "$NAME" --query KeyMaterial --output text > "./$NAME.pem"
echo "PRIVATE KEY written to ./$NAME.pem: move it to the password manager (outside this system) and delete it."

IID=$(aws ec2 run-instances --image-id "$AMI" --instance-type "$TYPE" \
  --key-name "$NAME" --security-group-ids "$SG" --subnet-id "$SUBNET" \
  --iam-instance-profile "Name=$NAME-profile" \
  --block-device-mappings '[{"DeviceName":"/dev/xvda","Ebs":{"VolumeSize":60,"VolumeType":"gp3","DeleteOnTermination":true,"Encrypted":true}}]' \
  --metadata-options 'HttpTokens=required,HttpEndpoint=enabled' \
  --user-data "file://$HERE/standby-userdata.sh" \
  --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$NAME},{Key=purpose,Value=cold-standby}]" \
  --query 'Instances[0].InstanceId' --output text)
aws ec2 wait instance-running --instance-ids "$IID"
IP=$(aws ec2 describe-instances --instance-ids "$IID" \
  --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)
cat <<EOF
instance=$IID ip=$IP
ssh -i ./$NAME.pem admin@$IP     # bootstrap log: /var/log/standby-bootstrap.log
When the log ends with BOOTSTRAP-OK, stop it:
aws ec2 stop-instances --instance-ids $IID
EOF

# The stopped instance, its network and its read-only role.

data "aws_region" "current" {}

data "aws_subnet" "standby" {
  id = var.subnet_id
}

# The newest official Debian 12 arm64 image, found the way provision-standby.sh
# finds it. 136693071363 is the account the Debian cloud team publishes from
# (wiki.debian.org/Cloud/AmazonEC2Image): pinning the owner keeps a look-alike
# community AMI out.
data "aws_ami" "debian" {
  owners      = ["136693071363"]
  most_recent = true

  filter {
    name   = "name"
    values = ["debian-12-arm64-*"]
  }

  filter {
    name   = "state"
    values = ["available"]
  }
}

locals {
  user_data = var.user_data != null ? var.user_data : replace(
    replace(file("${path.module}/../standby-userdata.sh"), local.placeholder, var.bucket_name),
    "/(?m)^REGION=.*$/", "REGION=${data.aws_region.current.region}",
  )
  ssh_cidrs = var.ssh_ingress_cidr == null ? [] : [var.ssh_ingress_cidr]
}

# SSH from the operator's address only, or no ingress at all. Everything else
# goes over the VPN. The rules are set inline and in attribute form on purpose:
# that makes this resource authoritative over them, even when the list is
# empty, so a rule added by hand shows up as drift in the next plan.
resource "aws_security_group" "standby" {
  name        = coalesce(var.security_group_name, var.name)
  description = var.security_group_description
  vpc_id      = data.aws_subnet.standby.vpc_id
  tags        = var.tags

  ingress = [for cidr in local.ssh_cidrs : {
    description      = ""
    protocol         = "tcp"
    from_port        = 22
    to_port          = 22
    cidr_blocks      = [cidr]
    ipv6_cidr_blocks = []
    prefix_list_ids  = []
    security_groups  = []
    self             = false
  }]

  # Outbound stays open, as on a new group: apt, S3 and the VPN need it.
  egress = [{
    description      = ""
    protocol         = "-1"
    from_port        = 0
    to_port          = 0
    cidr_blocks      = ["0.0.0.0/0"]
    ipv6_cidr_blocks = []
    prefix_list_ids  = []
    security_groups  = []
    self             = false
  }]
}

# Read-only role: the standby restores FROM the bucket and can never modify it.
# No access key lands on the machine; it gets short-lived role credentials.
resource "aws_iam_role" "standby" {
  name               = coalesce(var.role_name, "${var.name}-role")
  assume_role_policy = local.standby_trust
  tags               = var.tags
}

resource "aws_iam_role_policy" "standby_read" {
  name   = coalesce(var.read_policy_name, "${var.name}-read-backups")
  role   = aws_iam_role.standby.id
  policy = local.standby_read_policy
}

# Exclusive: an inline or managed policy added to the role by hand is drift,
# not a silent extra permission.
resource "aws_iam_role_policies_exclusive" "standby" {
  role_name    = aws_iam_role.standby.name
  policy_names = [aws_iam_role_policy.standby_read.name]
}

resource "aws_iam_role_policy_attachments_exclusive" "standby" {
  role_name   = aws_iam_role.standby.name
  policy_arns = []
}

resource "aws_iam_instance_profile" "standby" {
  name = coalesce(var.instance_profile_name, "${var.name}-profile")
  role = aws_iam_role.standby.name
  tags = var.tags
}

resource "aws_instance" "standby" {
  ami                    = data.aws_ami.debian.id
  instance_type          = var.instance_type
  subnet_id              = var.subnet_id
  vpc_security_group_ids = [aws_security_group.standby.id]
  key_name               = var.key_name # referenced by name, never managed: see variables.tf
  iam_instance_profile   = aws_iam_instance_profile.standby.name
  user_data              = local.user_data

  monitoring              = false
  ebs_optimized           = false
  disable_api_termination = false

  metadata_options {
    http_endpoint = "enabled"
    http_tokens   = "required" # IMDSv2 only
  }

  root_block_device {
    volume_type           = "gp3"
    volume_size           = var.root_volume_size
    iops                  = var.root_volume_iops
    throughput            = var.root_volume_throughput
    encrypted             = true
    kms_key_id            = var.root_volume_kms_key_id
    delete_on_termination = true
  }

  tags = merge(var.tags, { Name = var.name }, var.instance_tags)

  lifecycle {
    # The AMI lookup moves every time Debian publishes an image, and a new AMI
    # REPLACES the instance, losing the data staged on it. A user-data change
    # would stop and start it (or replace it, with user_data_replace_on_change)
    # for a script that only runs on the first boot. So a rebuild is never a
    # side effect of a plan; it is a deliberate operation:
    #   terraform apply -replace=<address of this resource>
    # tests/existing-bucket.tftest.hcl checks that a new image and new
    # user-data leave the planned instance alone.
    ignore_changes = [ami, user_data, user_data_base64]
  }
}

# Stopped at rest: it then costs only its volume, and the auto-assigned public
# IPv4 is released. A standby found running outside a drill is drift.
resource "aws_ec2_instance_state" "standby" {
  instance_id = aws_instance.standby.id
  state       = var.instance_state
}

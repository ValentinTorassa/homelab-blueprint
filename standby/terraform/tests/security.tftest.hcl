# The security properties of the standby, checked offline: a mock AWS provider,
# no credentials, plan only, nothing created. This file covers the default mode,
# where the module creates the bucket; existing-bucket.tftest.hcl the other.

mock_provider "aws" {
  # Not the region written in standby-userdata.sh, so the substitution shows.
  mock_data "aws_region" {
    defaults = { region = "sa-east-1" }
  }
  mock_data "aws_subnet" {
    defaults = { vpc_id = "vpc-0123456789abcdef0" }
  }
  mock_data "aws_ami" {
    defaults = { id = "ami-0123456789abcdef0" }
  }
}

# Everything else is left at its default, so the defaults are what is tested.
variables {
  bucket_name      = "example-standby-test-bucket"
  subnet_id        = "subnet-0123456789abcdef0"
  key_name         = "example-key"
  ssh_ingress_cidr = "203.0.113.10/32"
}

run "ssh_only_from_the_operator" {
  command = plan

  assert {
    condition     = length(aws_security_group.standby.ingress) == 1
    error_message = "Expected exactly one ingress rule, SSH."
  }

  assert {
    condition = alltrue([for r in aws_security_group.standby.ingress :
      r.protocol == "tcp" && r.from_port == 22 && r.to_port == 22 &&
      toset(r.cidr_blocks) == toset(["203.0.113.10/32"]) &&
      try(length(r.ipv6_cidr_blocks), 0) == 0 &&
      try(length(r.security_groups), 0) == 0 &&
      try(length(r.prefix_list_ids), 0) == 0 &&
      !coalesce(r.self, false)
    ])
    error_message = "The only ingress must be TCP 22 from ssh_ingress_cidr."
  }

  assert {
    condition = length(setintersection(
      toset(flatten([for r in aws_security_group.standby.ingress : [
        r.cidr_blocks == null ? [] : r.cidr_blocks,
        r.ipv6_cidr_blocks == null ? [] : r.ipv6_cidr_blocks,
      ]])),
      toset(["0.0.0.0/0", "::/0"]),
    )) == 0
    error_message = "No ingress may be open to the whole internet."
  }
}

run "no_ingress_without_an_operator_address" {
  command = plan

  variables {
    ssh_ingress_cidr = null
  }

  assert {
    condition     = length(aws_security_group.standby.ingress) == 0
    error_message = "With no ssh_ingress_cidr, no port may be open."
  }
}

run "instance_is_hardened_and_stopped" {
  command = plan

  assert {
    condition     = aws_instance.standby.metadata_options[0].http_tokens == "required"
    error_message = "IMDSv2 must be required."
  }

  assert {
    condition     = aws_instance.standby.metadata_options[0].http_endpoint == "enabled"
    error_message = "The metadata endpoint must stay on: the read-only role is delivered through it."
  }

  assert {
    condition = (
      aws_instance.standby.root_block_device[0].encrypted == true &&
      aws_instance.standby.root_block_device[0].volume_type == "gp3" &&
      aws_instance.standby.root_block_device[0].volume_size == 60 &&
      aws_instance.standby.root_block_device[0].iops == 3000 &&
      aws_instance.standby.root_block_device[0].throughput == 125
    )
    error_message = "The root volume must be an encrypted 60 GB gp3 at 3000 IOPS and 125 MiB/s."
  }

  assert {
    condition = (
      aws_instance.standby.monitoring == false &&
      aws_instance.standby.ebs_optimized == false &&
      aws_instance.standby.disable_api_termination == false
    )
    error_message = "Detailed monitoring, EBS optimization and termination protection must be set explicitly (off)."
  }

  assert {
    condition     = aws_instance.standby.tags == tomap({ Name = "standby", purpose = "cold-standby" })
    error_message = "The instance must be tagged Name and purpose, and nothing else by default."
  }

  assert {
    condition     = length(aws_security_group.standby.tags) == 0 && aws_security_group.standby.name == "standby" && aws_iam_role.standby.name == "standby-role"
    error_message = "The defaults must be neutral: untagged, standby-* names."
  }

  assert {
    condition     = aws_ec2_instance_state.standby.state == "stopped"
    error_message = "The standby must rest stopped."
  }

  assert {
    condition     = aws_instance.standby.instance_type == "t4g.medium" && aws_instance.standby.ami == data.aws_ami.debian.id
    error_message = "Expected t4g.medium on the looked-up Debian image."
  }

  assert {
    condition = (
      tolist(data.aws_ami.debian.owners) == tolist(["136693071363"]) &&
      data.aws_ami.debian.most_recent == true &&
      anytrue([for f in data.aws_ami.debian.filter : f.name == "name" && toset(f.values) == toset(["debian-12-arm64-*"])])
    )
    error_message = "The AMI must be the newest debian-12-arm64 image from the official Debian account."
  }

  assert {
    condition     = aws_instance.standby.iam_instance_profile == aws_iam_instance_profile.standby.name && aws_iam_instance_profile.standby.role == aws_iam_role.standby.name
    error_message = "The instance must carry the read-only role, through its instance profile."
  }

  assert {
    condition = (
      strcontains(aws_instance.standby.user_data, "BUCKET=example-standby-test-bucket\n") &&
      strcontains(aws_instance.standby.user_data, "\nREGION=sa-east-1\n") &&
      !strcontains(aws_instance.standby.user_data, "REGION=us-east-1") &&
      !strcontains(aws_instance.standby.user_data, "example-offsite-bucket")
    )
    error_message = "The bootstrap must sync from this bucket, in this region."
  }
}

run "bucket_keeps_its_history" {
  command = plan

  assert {
    condition     = aws_s3_bucket.offsite[0].object_lock_enabled == true && aws_s3_bucket.offsite[0].force_destroy == false
    error_message = "The bucket needs Object Lock and must not be force-destroyable."
  }

  assert {
    condition     = aws_s3_bucket_versioning.offsite[0].versioning_configuration[0].status == "Enabled"
    error_message = "Versioning must be on."
  }

  assert {
    condition = (
      aws_s3_bucket_object_lock_configuration.offsite[0].rule[0].default_retention[0].mode == "GOVERNANCE" &&
      aws_s3_bucket_object_lock_configuration.offsite[0].rule[0].default_retention[0].days == 30
    )
    error_message = "Default retention must follow object_lock_mode and object_lock_days."
  }

  assert {
    condition = (
      aws_s3_bucket_public_access_block.offsite[0].block_public_acls &&
      aws_s3_bucket_public_access_block.offsite[0].block_public_policy &&
      aws_s3_bucket_public_access_block.offsite[0].ignore_public_acls &&
      aws_s3_bucket_public_access_block.offsite[0].restrict_public_buckets
    )
    error_message = "All four public access blocks must be on."
  }

  assert {
    condition     = one(aws_s3_bucket_ownership_controls.offsite[0].rule).object_ownership == "BucketOwnerEnforced"
    error_message = "ACLs must be disabled (BucketOwnerEnforced)."
  }

  assert {
    condition = (
      one(one(aws_s3_bucket_server_side_encryption_configuration.offsite[0].rule).apply_server_side_encryption_by_default).sse_algorithm == "AES256" &&
      one(aws_s3_bucket_server_side_encryption_configuration.offsite[0].rule).bucket_key_enabled == true
    )
    error_message = "Without a KMS key, default encryption must be SSE-S3, with the bucket key on."
  }
}

run "bucket_uses_kms_when_given_a_key" {
  command = plan

  variables {
    sse_kms_key_arn = "arn:aws:kms:us-east-1:111122223333:key/00000000-0000-0000-0000-000000000000"
  }

  assert {
    condition = (
      one(one(aws_s3_bucket_server_side_encryption_configuration.offsite[0].rule).apply_server_side_encryption_by_default).sse_algorithm == "aws:kms" &&
      one(one(aws_s3_bucket_server_side_encryption_configuration.offsite[0].rule).apply_server_side_encryption_by_default).kms_master_key_id == var.sse_kms_key_arn &&
      one(aws_s3_bucket_server_side_encryption_configuration.offsite[0].rule).bucket_key_enabled == true
    )
    error_message = "With a KMS key, default encryption must be SSE-KMS with a bucket key."
  }
}

run "standby_role_is_read_only_on_the_bucket" {
  command = plan

  assert {
    condition = alltrue([for s in jsondecode(aws_iam_role_policy.standby_read.policy).Statement :
      s.Effect == "Allow" &&
      length(setsubtract(flatten([s.Action]), ["s3:ListBucket", "s3:GetBucketLocation", "s3:GetObject", "s3:RestoreObject"])) == 0
    ])
    error_message = "The standby role may only list, locate, read and thaw: no write, delete or wildcard action."
  }

  assert {
    condition = alltrue([for s in jsondecode(aws_iam_role_policy.standby_read.policy).Statement :
      length(setsubtract(flatten([s.Resource]), ["arn:aws:s3:::example-standby-test-bucket", "arn:aws:s3:::example-standby-test-bucket/*"])) == 0
    ])
    error_message = "The standby role may only reach the offsite bucket."
  }

  assert {
    condition = (
      length(jsondecode(aws_iam_role.standby.assume_role_policy).Statement) == 1 &&
      jsondecode(aws_iam_role.standby.assume_role_policy).Statement[0].Principal == { Service = "ec2.amazonaws.com" } &&
      jsondecode(aws_iam_role.standby.assume_role_policy).Statement[0].Action == "sts:AssumeRole"
    )
    error_message = "Only EC2 may assume the standby role."
  }

  assert {
    condition = (
      length(aws_iam_role_policy_attachments_exclusive.standby.policy_arns) == 0 &&
      aws_iam_role_policies_exclusive.standby.policy_names == toset([aws_iam_role_policy.standby_read.name])
    )
    error_message = "The role must hold exactly the read policy, and no managed policy."
  }
}

run "uploader_cannot_erase_history" {
  command = plan

  assert {
    condition = length(setsubtract(
      ["s3:DeleteObject", "s3:DeleteObjectVersion", "s3:PutObjectRetention", "s3:PutObjectLegalHold",
        "s3:PutBucketObjectLockConfiguration", "s3:PutBucketVersioning", "s3:PutLifecycleConfiguration",
      "s3:PutBucketPolicy", "s3:DeleteBucket"],
      flatten([for s in jsondecode(aws_iam_policy.uploader[0].policy).Statement : s.Action if s.Effect == "Deny"])
    )) == 0
    error_message = "The uploader must be explicitly denied every delete and every retention, versioning, lifecycle and policy change."
  }

  assert {
    condition = alltrue([for s in jsondecode(aws_iam_policy.uploader[0].policy).Statement :
      contains(flatten([s.Resource]), "arn:aws:s3:::example-standby-test-bucket/*") if s.Effect == "Deny"
    ])
    error_message = "The Deny must cover the objects of this bucket."
  }

  assert {
    condition = alltrue([for a in flatten([for s in jsondecode(aws_iam_policy.uploader[0].policy).Statement : s.Action if s.Effect == "Allow"]) :
      !can(regex("Delete|Retention|LegalHold|ObjectLock|Versioning|Lifecycle|Policy|\\*", a))
    ])
    error_message = "The uploader's Allow must not include deletes, retention or policy changes, or wildcards."
  }

  assert {
    condition     = !strcontains(aws_iam_policy.uploader[0].policy, "example-offsite-bucket") && !strcontains(aws_iam_role_policy.standby_read.policy, "example-offsite-bucket")
    error_message = "The placeholder bucket must be replaced in every policy."
  }
}

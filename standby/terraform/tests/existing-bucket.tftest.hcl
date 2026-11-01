# create_bucket = false: the bucket already exists and stays outside Terraform.
# Nothing touches it, and the standby keeps the same guarantees.

mock_provider "aws" {
  mock_data "aws_region" {
    defaults = { region = "us-east-1" }
  }
  mock_data "aws_subnet" {
    defaults = { vpc_id = "vpc-0123456789abcdef0" }
  }
  mock_data "aws_ami" {
    defaults = { id = "ami-0123456789abcdef0" }
  }
}

variables {
  create_bucket    = false
  bucket_name      = "example-existing-bucket"
  subnet_id        = "subnet-0123456789abcdef0"
  key_name         = "example-key"
  ssh_ingress_cidr = "203.0.113.10/32"
}

run "plans_no_bucket_resource" {
  command = plan

  assert {
    condition = (
      length(aws_s3_bucket.offsite) == 0 &&
      length(aws_s3_bucket_versioning.offsite) == 0 &&
      length(aws_s3_bucket_object_lock_configuration.offsite) == 0 &&
      length(aws_s3_bucket_server_side_encryption_configuration.offsite) == 0 &&
      length(aws_s3_bucket_public_access_block.offsite) == 0 &&
      length(aws_s3_bucket_ownership_controls.offsite) == 0
    )
    error_message = "With create_bucket = false, no bucket resource may be planned."
  }

  assert {
    condition     = output.bucket_name == "example-existing-bucket" && output.bucket_arn == "arn:aws:s3:::example-existing-bucket"
    error_message = "The outputs must name the existing bucket."
  }
}

run "standby_is_still_read_only_on_that_bucket" {
  command = plan

  assert {
    condition = alltrue([for s in jsondecode(aws_iam_role_policy.standby_read.policy).Statement :
      s.Effect == "Allow" &&
      length(setsubtract(flatten([s.Action]), ["s3:ListBucket", "s3:GetBucketLocation", "s3:GetObject", "s3:RestoreObject"])) == 0 &&
      length(setsubtract(flatten([s.Resource]), ["arn:aws:s3:::example-existing-bucket", "arn:aws:s3:::example-existing-bucket/*"])) == 0
    ])
    error_message = "The standby role may only list, locate, read and thaw the existing bucket."
  }

  assert {
    condition     = strcontains(aws_instance.standby.user_data, "BUCKET=example-existing-bucket\n")
    error_message = "The bootstrap must sync from the existing bucket."
  }

  assert {
    condition = (
      aws_instance.standby.metadata_options[0].http_tokens == "required" &&
      aws_instance.standby.root_block_device[0].encrypted == true &&
      aws_ec2_instance_state.standby.state == "stopped" &&
      length(aws_security_group.standby.ingress) == 1
    )
    error_message = "The standby keeps IMDSv2, encryption, the stopped state and SSH only, whatever the bucket mode."
  }
}

run "uploader_policy_still_denies_deletes" {
  command = plan

  assert {
    condition = length(setsubtract(
      ["s3:DeleteObject", "s3:DeleteObjectVersion", "s3:PutObjectRetention", "s3:PutBucketObjectLockConfiguration", "s3:PutBucketVersioning"],
      flatten([for s in jsondecode(aws_iam_policy.uploader[0].policy).Statement : s.Action if s.Effect == "Deny"])
    )) == 0
    error_message = "The uploader policy must deny deletes and retention changes on the existing bucket too."
  }

  assert {
    condition     = strcontains(aws_iam_policy.uploader[0].policy, "arn:aws:s3:::example-existing-bucket/*")
    error_message = "The uploader policy must be built for the existing bucket."
  }
}

run "no_uploader_policy_when_managed_elsewhere" {
  command = plan

  variables {
    create_uploader_policy = false
  }

  assert {
    condition     = length(aws_iam_policy.uploader) == 0 && output.uploader_policy_arn == null
    error_message = "With create_uploader_policy = false, no uploader policy may be planned."
  }
}

run "names_match_what_exists" {
  command = plan

  variables {
    security_group_name        = "example-sg"
    security_group_description = "example description"
    role_name                  = "example-role"
    read_policy_name           = "example-read"
    instance_profile_name      = "example-profile"
    uploader_policy_name       = "example-uploader"
  }

  assert {
    condition = (
      aws_security_group.standby.name == "example-sg" &&
      aws_security_group.standby.description == "example description" &&
      aws_iam_role.standby.name == "example-role" &&
      aws_iam_role_policy.standby_read.name == "example-read" &&
      aws_iam_instance_profile.standby.name == "example-profile" &&
      aws_iam_policy.uploader[0].name == "example-uploader" &&
      aws_instance.standby.key_name == "example-key"
    )
    error_message = "Every name input must reach its resource, so an import can match what exists."
  }

  assert {
    condition = (
      aws_instance.standby.iam_instance_profile == "example-profile" &&
      aws_iam_instance_profile.standby.role == "example-role" &&
      aws_iam_role_policies_exclusive.standby.policy_names == toset(["example-read"])
    )
    error_message = "Renamed pieces must still be wired to each other."
  }
}

# The two runs below apply against the mock provider (nothing is created
# anywhere) and then plan again with a newer Debian image and different
# user-data. Neither may touch the instance: replacing it would throw away the
# data staged on it. In this mode no bucket has prevent_destroy, so the mock
# state can be torn down afterwards.
run "provisioned" {
  command = apply
}

run "a_new_image_or_new_user_data_is_not_a_rebuild" {
  command = plan

  variables {
    user_data = "#!/bin/bash\necho a later edit\n"
  }

  override_data {
    target = data.aws_ami.debian
    values = { id = "ami-0fedcba9876543210" }
  }

  assert {
    condition     = data.aws_ami.debian.id == "ami-0fedcba9876543210" && aws_instance.standby.ami == "ami-0123456789abcdef0"
    error_message = "A newer Debian image must not change the planned instance."
  }

  assert {
    condition     = strcontains(aws_instance.standby.user_data, "BUCKET=example-existing-bucket\n")
    error_message = "Edited user-data must not change the planned instance."
  }

  assert {
    condition     = aws_instance.standby.id == run.provisioned.instance_id
    error_message = "The instance must keep its ID: no replacement."
  }
}


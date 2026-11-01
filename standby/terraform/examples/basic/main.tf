# A root module that builds the whole standby from the module one level up.
# The values come from terraform.tfvars (git-ignored): copy
# terraform.tfvars.example and replace every value with your own.
#
# There is no backend block, so the state is a local file. It holds every real
# identifier (account, bucket, instance, your IP), so wherever you keep it,
# keep it out of a public repository.

terraform {
  required_version = ">= 1.9.0, < 2.0.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.71"
    }
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = { project = "homelab-standby", managed-by = "terraform" }
  }
}

module "standby" {
  source = "../.."

  bucket_name      = var.bucket_name
  object_lock_mode = var.object_lock_mode
  object_lock_days = var.object_lock_days
  subnet_id        = var.subnet_id
  key_name         = var.key_name
  ssh_ingress_cidr = var.ssh_ingress_cidr
  instance_state   = var.instance_state
}

output "instance_id" {
  value = module.standby.instance_id
}

output "uploader_policy_arn" {
  value = module.standby.uploader_policy_arn
}

# Every value that identifies an account (bucket, subnet, key pair, operator
# IP) is an input with no default. The region comes from the caller's provider.

# --- The offsite bucket ------------------------------------------------------

variable "bucket_name" {
  description = "The offsite bucket. It replaces the example-offsite-bucket placeholder in ../iam/*.json and ../standby-userdata.sh, as provision-standby.sh does with sed."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", var.bucket_name)) && !strcontains(var.bucket_name, "..")
    error_message = "bucket_name must be a valid S3 bucket name: 3-63 lowercase letters, digits, dots and hyphens."
  }
}

variable "create_bucket" {
  description = "true creates the bucket with versioning, Object Lock, SSE and the public access block. false leaves an existing bucket outside Terraform: the policies are still built for bucket_name, and no bucket resource is planned."
  type        = bool
  default     = true
}

variable "object_lock_mode" {
  description = "Default Object Lock retention mode for new object versions: GOVERNANCE (an identity with s3:BypassGovernanceRetention can still remove a version) or COMPLIANCE (nobody can, root included, until it expires). Only used when create_bucket is true."
  type        = string
  default     = "GOVERNANCE"

  validation {
    condition     = var.object_lock_mode == null ? !var.create_bucket : contains(["GOVERNANCE", "COMPLIANCE"], var.object_lock_mode)
    error_message = "object_lock_mode must be GOVERNANCE or COMPLIANCE when create_bucket is true."
  }
}

variable "object_lock_days" {
  description = "Default Object Lock retention, in days, for every new object version. Only used when create_bucket is true."
  type        = number
  default     = 30

  validation {
    condition     = var.object_lock_days == null ? !var.create_bucket : (var.object_lock_days >= 1 && var.object_lock_days <= 36500 && floor(var.object_lock_days) == var.object_lock_days)
    error_message = "object_lock_days must be a whole number of days between 1 and 36500 when create_bucket is true."
  }
}

variable "sse_kms_key_arn" {
  description = "KMS key for the bucket's default encryption. Null keeps SSE-S3 (AES256). Only used when create_bucket is true."
  type        = string
  default     = null

  validation {
    condition     = var.sse_kms_key_arn == null || can(regex("^arn:aws[a-z-]*:kms:", var.sse_kms_key_arn))
    error_message = "sse_kms_key_arn must be a KMS key ARN, or null for SSE-S3."
  }
}

variable "create_uploader_policy" {
  description = "true creates the uploader's managed policy from ../iam/backup-uploader.json. false creates none, for an uploader whose policy is managed elsewhere."
  type        = bool
  default     = true
}

# --- Names ------------------------------------------------------------------
# Each one defaults to a name derived from `name`. Set them to match existing
# resources exactly before importing: a different name means a replacement.

variable "name" {
  description = "Prefix for the default names below, and the instance's Name tag."
  type        = string
  default     = "standby"

  validation {
    condition     = can(regex("^[A-Za-z0-9+=,.@_-]{1,48}$", var.name))
    error_message = "name must be 1-48 characters of letters, digits and +=,.@_- (it prefixes IAM names)."
  }
}

variable "security_group_name" {
  description = "Security group name. Null: <name>."
  type        = string
  default     = null

  validation {
    condition     = var.security_group_name == null || can(regex("^[A-Za-z0-9 ._:/()#,@+=&;{}!$*-]{1,255}$", var.security_group_name))
    error_message = "security_group_name must be 1-255 characters that EC2 accepts in a group name."
  }
}

variable "security_group_description" {
  description = "Security group description. EC2 cannot change it in place, so match the existing group's when importing."
  type        = string
  default     = "Cold standby: SSH from the operator IP only, everything else over the VPN"
}

variable "role_name" {
  description = "Instance role name. Null: <name>-role."
  type        = string
  default     = null

  validation {
    condition     = var.role_name == null || can(regex("^[A-Za-z0-9+=,.@_-]{1,64}$", var.role_name))
    error_message = "role_name must be 1-64 characters of letters, digits and +=,.@_-."
  }
}

variable "read_policy_name" {
  description = "Name of the role's inline read-only policy. Null: <name>-read-backups."
  type        = string
  default     = null

  validation {
    condition     = var.read_policy_name == null || can(regex("^[A-Za-z0-9+=,.@_-]{1,128}$", var.read_policy_name))
    error_message = "read_policy_name must be 1-128 characters of letters, digits and +=,.@_-."
  }
}

variable "instance_profile_name" {
  description = "Instance profile name. Null: <name>-profile."
  type        = string
  default     = null

  validation {
    condition     = var.instance_profile_name == null || can(regex("^[A-Za-z0-9+=,.@_-]{1,128}$", var.instance_profile_name))
    error_message = "instance_profile_name must be 1-128 characters of letters, digits and +=,.@_-."
  }
}

variable "uploader_policy_name" {
  description = "Name of the uploader's managed policy. Null: <name>-backup-uploader."
  type        = string
  default     = null

  validation {
    condition     = var.uploader_policy_name == null || can(regex("^[A-Za-z0-9+=,.@_-]{1,128}$", var.uploader_policy_name))
    error_message = "uploader_policy_name must be 1-128 characters of letters, digits and +=,.@_-."
  }
}

variable "key_name" {
  description = "An EC2 key pair that already exists, referenced by name. Its private key lives in the password manager, outside the homelab; Terraform never generates or manages one, so no private key reaches the state."
  type        = string

  validation {
    condition     = length(var.key_name) >= 1 && length(var.key_name) <= 255
    error_message = "key_name must be the name of an existing EC2 key pair."
  }
}

# --- The instance and its network ---------------------------------------------

variable "subnet_id" {
  description = "Subnet for the standby. It needs a route out (apt, S3, the VPN). The security group goes in the same VPC."
  type        = string

  validation {
    condition     = can(regex("^subnet-[0-9a-f]{8,17}$", var.subnet_id))
    error_message = "subnet_id must look like subnet-0123456789abcdef0."
  }
}

variable "ssh_ingress_cidr" {
  description = "Where SSH may come from: the operator's address as a /32. Null opens no port at all. Wider than a /16, host bits set, IPv6 or 0.0.0.0/0 are rejected."
  type        = string
  default     = null

  validation {
    condition     = var.ssh_ingress_cidr == null || try(!strcontains(var.ssh_ingress_cidr, ":") && cidrsubnet(var.ssh_ingress_cidr, 0, 0) == var.ssh_ingress_cidr, false)
    error_message = "ssh_ingress_cidr must be one IPv4 network in CIDR form with no host bits set, like 203.0.113.10/32."
  }

  validation {
    condition     = var.ssh_ingress_cidr == null || try(tonumber(split("/", var.ssh_ingress_cidr)[1]) > 16, false)
    error_message = "ssh_ingress_cidr must be narrower than a /16: a single host (/32) is the intended value, and 0.0.0.0/0 is never accepted."
  }
}

variable "instance_type" {
  description = "Resting instance type. It must be arm64 (Graviton), because the AMI is. takeover-drill.sh falls back to other arm64 types and puts this one back."
  type        = string
  default     = "t4g.medium"

  validation {
    condition     = can(regex("^[a-z]+[0-9]+g[a-z0-9-]*\\.[a-z0-9]+$", var.instance_type))
    error_message = "instance_type must be an arm64 (Graviton) type such as t4g.medium or m7g.medium."
  }
}

variable "root_volume_size" {
  description = "Size of the encrypted gp3 root volume, in GiB."
  type        = number
  default     = 60

  validation {
    condition     = var.root_volume_size >= 8 && floor(var.root_volume_size) == var.root_volume_size
    error_message = "root_volume_size must be a whole number of GiB, 8 or more."
  }
}

variable "root_volume_iops" {
  description = "Provisioned IOPS of the gp3 root volume. 3000 is the gp3 baseline."
  type        = number
  default     = 3000

  validation {
    condition     = var.root_volume_iops >= 3000 && floor(var.root_volume_iops) == var.root_volume_iops
    error_message = "root_volume_iops must be a whole number, 3000 (the gp3 baseline) or more."
  }
}

variable "root_volume_throughput" {
  description = "Throughput of the gp3 root volume, in MiB/s. 125 is the gp3 baseline."
  type        = number
  default     = 125

  validation {
    condition     = var.root_volume_throughput >= 125 && floor(var.root_volume_throughput) == var.root_volume_throughput
    error_message = "root_volume_throughput must be a whole number of MiB/s, 125 (the gp3 baseline) or more."
  }
}

variable "root_volume_kms_key_id" {
  description = "KMS key for the root volume. Null encrypts it with the AWS-managed EBS key (aws/ebs). The volume is encrypted either way."
  type        = string
  default     = null
}

variable "instance_state" {
  description = "stopped is the resting state. Use running only for the first apply, so the bootstrap can finish (BOOTSTRAP-OK in /var/log/standby-bootstrap.log), then apply again with stopped."
  type        = string
  default     = "stopped"

  validation {
    condition     = contains(["stopped", "running"], var.instance_state)
    error_message = "instance_state must be stopped or running."
  }
}

variable "user_data" {
  description = "Bootstrap script. Null renders ../standby-userdata.sh with this bucket and region. It runs on the first boot only, so later changes do not touch an existing standby."
  type        = string
  default     = null
}

variable "tags" {
  description = "Tags for every resource that takes them. Empty by default, so nothing is tagged that was not tagged before an import."
  type        = map(string)
  default     = {}
}

variable "instance_tags" {
  description = "Tags for the instance only, on top of Name = <name>. Set them to the existing instance's tags exactly before importing (Name included, if it differs from name)."
  type        = map(string)
  default     = { purpose = "cold-standby" }
}

variable "region" {
  description = "AWS region for everything: the bucket, the role and the standby."
  type        = string
}

variable "bucket_name" {
  description = "The offsite bucket's name (globally unique)."
  type        = string
}

variable "object_lock_mode" {
  description = "GOVERNANCE or COMPLIANCE."
  type        = string
}

variable "object_lock_days" {
  description = "Default retention for every new object version, in days."
  type        = number
}

variable "subnet_id" {
  description = "A subnet with a route out, for the standby."
  type        = string
}

variable "key_name" {
  description = "An existing EC2 key pair."
  type        = string
}

variable "ssh_ingress_cidr" {
  description = "Your public address as a /32, or null to open no port."
  type        = string
  default     = null
}

variable "instance_state" {
  description = "running for the first apply only, until the bootstrap finishes; then stopped."
  type        = string
  default     = "stopped"
}

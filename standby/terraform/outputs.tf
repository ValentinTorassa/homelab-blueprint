# Identifiers only: no IP address, key material or policy text.

output "instance_id" {
  description = "The standby instance, for takeover-drill.sh (STANDBY_INSTANCE_ID)."
  value       = aws_instance.standby.id
}

output "security_group_id" {
  description = "The standby's security group."
  value       = aws_security_group.standby.id
}

output "bucket_name" {
  description = "The offsite bucket, created here or passed in."
  value       = var.create_bucket ? aws_s3_bucket.offsite[0].id : var.bucket_name
}

output "bucket_arn" {
  description = "The offsite bucket's ARN, created here or built from its name."
  value       = var.create_bucket ? aws_s3_bucket.offsite[0].arn : "arn:aws:s3:::${var.bucket_name}"
}

output "standby_role_name" {
  description = "The standby's read-only instance role."
  value       = aws_iam_role.standby.name
}

output "instance_profile_name" {
  description = "The instance profile that carries the role."
  value       = aws_iam_instance_profile.standby.name
}

output "uploader_policy_arn" {
  description = "Attach this to the identity the home uploader uses. Null when create_uploader_policy is false."
  value       = one(aws_iam_policy.uploader[*].arn)
}

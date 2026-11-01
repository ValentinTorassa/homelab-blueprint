# The offsite bucket, and the policy for the one credential that writes to it.

locals {
  # The policy documents and the bootstrap script are the same files
  # provision-standby.sh reads. Both paths replace the same placeholder with the
  # real bucket, so the bash path and this one cannot grant different things.
  placeholder = "example-offsite-bucket"
  iam_dir     = "${path.module}/../iam"

  uploader_policy     = jsonencode(jsondecode(replace(file("${local.iam_dir}/backup-uploader.json"), local.placeholder, var.bucket_name)))
  standby_read_policy = jsonencode(jsondecode(replace(file("${local.iam_dir}/standby-read-only.json"), local.placeholder, var.bucket_name)))
  standby_trust       = jsonencode(jsondecode(file("${local.iam_dir}/standby-trust.json")))

  bucket = var.create_bucket ? 1 : 0
}

# With create_bucket = false none of these is planned: an existing bucket, and
# whatever else is configured on it, stays outside Terraform.

resource "aws_s3_bucket" "offsite" {
  count = local.bucket

  bucket              = var.bucket_name
  object_lock_enabled = true
  force_destroy       = false
  tags                = var.tags

  # The bucket is the history. Everything else here can be rebuilt from code.
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_ownership_controls" "offsite" {
  count  = local.bucket
  bucket = aws_s3_bucket.offsite[0].id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "offsite" {
  count  = local.bucket
  bucket = aws_s3_bucket.offsite[0].id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_versioning" "offsite" {
  count  = local.bucket
  bucket = aws_s3_bucket.offsite[0].id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_object_lock_configuration" "offsite" {
  count  = local.bucket
  bucket = aws_s3_bucket.offsite[0].id

  rule {
    default_retention {
      mode = var.object_lock_mode
      days = var.object_lock_days
    }
  }

  depends_on = [aws_s3_bucket_versioning.offsite]
}

resource "aws_s3_bucket_server_side_encryption_configuration" "offsite" {
  count  = local.bucket
  bucket = aws_s3_bucket.offsite[0].id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = var.sse_kms_key_arn == null ? "AES256" : "aws:kms"
      kms_master_key_id = var.sse_kms_key_arn
    }
    bucket_key_enabled = true
  }
}

# Put and read back, with an explicit Deny on delete, retention and policy
# changes. Attach it to whatever identity the home uploader uses. That identity
# and its access key stay outside Terraform, so no secret lands in the state.
resource "aws_iam_policy" "uploader" {
  count = var.create_uploader_policy ? 1 : 0

  name   = coalesce(var.uploader_policy_name, "${var.name}-backup-uploader")
  policy = local.uploader_policy
  tags   = var.tags
}

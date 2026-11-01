terraform {
  # CI runs fmt, validate and test with the exact version pinned in
  # .github/workflows/ci.yml; .terraform.lock.hcl pins the provider build.
  required_version = ">= 1.9.0, < 2.0.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.0.0, < 7.0.0"
    }
  }
}

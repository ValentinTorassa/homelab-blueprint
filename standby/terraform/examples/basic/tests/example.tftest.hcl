# The example values pass every input check in the module and produce a plan.
# Offline, with a mock provider:
#
#   terraform test -var-file=terraform.tfvars.example

mock_provider "aws" {
  mock_data "aws_region" {
    defaults = { region = "us-east-1" }
  }
  mock_data "aws_subnet" {
    defaults = { vpc_id = "vpc-0123456789abcdef0" }
  }
}

run "example_values_plan" {
  command = plan
}

# The inputs refuse values that would weaken the standby, before any plan.

mock_provider "aws" {
  mock_data "aws_region" {
    defaults = { region = "us-east-1" }
  }
  mock_data "aws_subnet" {
    defaults = { vpc_id = "vpc-0123456789abcdef0" }
  }
}

variables {
  bucket_name      = "example-standby-test-bucket"
  subnet_id        = "subnet-0123456789abcdef0"
  key_name         = "example-key"
  ssh_ingress_cidr = "203.0.113.10/32"
  object_lock_mode = "GOVERNANCE"
  object_lock_days = 30
}

run "accepts_a_narrow_range" {
  command = plan

  variables {
    ssh_ingress_cidr = "203.0.113.0/24"
  }
}

run "rejects_ssh_from_anywhere" {
  command = plan

  variables {
    ssh_ingress_cidr = "0.0.0.0/0"
  }

  expect_failures = [var.ssh_ingress_cidr]
}

run "rejects_a_slash_16" {
  command = plan

  variables {
    ssh_ingress_cidr = "198.51.0.0/16"
  }

  expect_failures = [var.ssh_ingress_cidr]
}

run "rejects_host_bits" {
  command = plan

  variables {
    ssh_ingress_cidr = "203.0.113.10/24"
  }

  expect_failures = [var.ssh_ingress_cidr]
}

run "rejects_a_bare_address" {
  command = plan

  variables {
    ssh_ingress_cidr = "203.0.113.10"
  }

  expect_failures = [var.ssh_ingress_cidr]
}

run "rejects_ipv6" {
  command = plan

  variables {
    ssh_ingress_cidr = "::/0"
  }

  expect_failures = [var.ssh_ingress_cidr]
}

run "rejects_an_unknown_lock_mode" {
  command = plan

  variables {
    object_lock_mode = "governance"
  }

  expect_failures = [var.object_lock_mode]
}

run "requires_a_lock_mode_when_creating_the_bucket" {
  command = plan

  variables {
    object_lock_mode = null
  }

  expect_failures = [var.object_lock_mode]
}

run "needs_no_lock_settings_for_an_existing_bucket" {
  command = plan

  variables {
    create_bucket    = false
    object_lock_mode = null
    object_lock_days = null
  }
}

run "rejects_zero_retention" {
  command = plan

  variables {
    object_lock_days = 0
  }

  expect_failures = [var.object_lock_days]
}

run "rejects_an_x86_type" {
  command = plan

  variables {
    instance_type = "t3.medium"
  }

  expect_failures = [var.instance_type]
}

run "rejects_an_unknown_state" {
  command = plan

  variables {
    instance_state = "terminated"
  }

  expect_failures = [var.instance_state]
}

run "runs_only_when_asked" {
  command = plan

  variables {
    instance_state = "running"
  }

  assert {
    condition     = aws_ec2_instance_state.standby.state == "running"
    error_message = "instance_state = running must reach the instance state resource."
  }
}

run "takes_its_bootstrap_when_given_one" {
  command = plan

  variables {
    user_data = "#!/bin/bash\necho custom\n"
  }

  assert {
    condition     = aws_instance.standby.user_data == "#!/bin/bash\necho custom\n"
    error_message = "A user_data input must be passed through untouched."
  }
}

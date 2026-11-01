# The standby as Terraform

A module that builds the cold standby described in [`../README.md`](../README.md):
the offsite bucket, the uploader's policy, the read-only role, the security
group and the stopped instance. It reads the same files as
[`../provision-standby.sh`](../provision-standby.sh): the policies in
[`../iam/`](../iam/) and the bootstrap script
[`../standby-userdata.sh`](../standby-userdata.sh). Both paths replace the
same `example-offsite-bucket` placeholder, so they cannot grant different
permissions.

| Piece | Resources | Held by the tests |
|---|---|---|
| Offsite bucket (`create_bucket = true`) | `aws_s3_bucket` + versioning, Object Lock, SSE, public access block, ownership controls | Versioning on; Object Lock on, GOVERNANCE 30 days unless set; all four public access blocks; ACLs off; SSE-S3, or SSE-KMS, with the bucket key on |
| Uploader | `aws_iam_policy` from [`backup-uploader.json`](../iam/backup-uploader.json) | Explicit Deny on every delete and on retention, versioning, lifecycle and policy changes; no such action in its Allow |
| Standby role | role, inline policy from [`standby-read-only.json`](../iam/standby-read-only.json), instance profile, exclusive policy lists | Only list, locate, read and thaw, only on this bucket; only EC2 can assume it; no other inline or managed policy on the role |
| Network | `aws_security_group`, rules inline | One ingress at most: TCP 22 from `ssh_ingress_cidr`; nothing from `0.0.0.0/0` or `::/0`; no ingress at all without an address |
| Instance | `aws_instance` + `aws_ec2_instance_state` | Newest `debian-12-arm64` from the Debian account; IMDSv2 required; encrypted 60 GB gp3 at 3000 IOPS and 125 MiB/s; stopped; a new image or new user-data does not rebuild it |

The inputs refuse what would weaken it: SSH from a range of /16 or wider,
host bits set, IPv6 or `0.0.0.0/0`; an x86 instance type; an unknown Object
Lock mode; zero retention.

## Two modes for the bucket

- `create_bucket = true`, the default: the module creates the bucket with
  everything above. That is the mode of the example and of a fresh copy.
- `create_bucket = false`: the bucket already exists and stays **outside**
  Terraform. The module plans no bucket resource, so nothing configured on
  that bucket can be changed or removed by a plan. The role policy, the
  uploader policy and the bootstrap are still built for `bucket_name`, and
  `bucket_arn` is built from it.

`create_uploader_policy = false` likewise leaves the uploader's policy to
whatever manages it today. `uploader_policy_arn` is then null.

## Try it offline

```bash
make terraform    # fmt -check, validate, terraform test: module and example
```

`terraform init` downloads the AWS provider from the registry. Everything else
runs against `mock_provider "aws"`. The Makefile drops every `AWS_*` variable
and points the profile and credential files at `/dev/null`, so no command can
reach an account even on a machine that has one configured. CI runs the same
target with no credentials at all.

The tests cover both bucket modes: [`tests/security.tftest.hcl`](tests/security.tftest.hcl)
(created), [`tests/existing-bucket.tftest.hcl`](tests/existing-bucket.tftest.hcl)
(passed in, renamed resources, and the no-rebuild check) and
[`tests/inputs.tftest.hcl`](tests/inputs.tftest.hcl) (what the inputs refuse).

## Inputs

| Input | Default | |
|---|---|---|
| `bucket_name` | required | Replaces the placeholder in the policies and the bootstrap |
| `subnet_id` | required | The security group goes in its VPC |
| `key_name` | required | An existing key pair, referenced by name (see below) |
| `ssh_ingress_cidr` | `null`: no port open | The operator's address, as a /32 |
| `create_bucket` | `true` | |
| `object_lock_mode`, `object_lock_days` | `GOVERNANCE`, `30` | Only with `create_bucket` |
| `sse_kms_key_arn` | `null`: SSE-S3 | Only with `create_bucket` |
| `create_uploader_policy` | `true` | |
| `name` | `standby` | Prefix of the default names below, and the instance's `Name` tag |
| `security_group_name`, `security_group_description` | `<name>`, a fixed sentence | EC2 cannot change either in place |
| `role_name`, `read_policy_name`, `instance_profile_name`, `uploader_policy_name` | `<name>-role`, `<name>-read-backups`, `<name>-profile`, `<name>-backup-uploader` | |
| `instance_type` | `t4g.medium` | arm64 only |
| `root_volume_size`, `root_volume_iops`, `root_volume_throughput` | `60`, `3000`, `125` | GiB, IOPS, MiB/s |
| `root_volume_kms_key_id` | `null`: the AWS-managed EBS key | Encrypted either way |
| `instance_state` | `stopped` | `running` only for the first apply |
| `user_data` | `null`: render `../standby-userdata.sh` | |
| `instance_tags` | `{ purpose = "cold-standby" }` | On top of `Name = <name>` |
| `tags` | `{}` | Every other resource |

The region is the caller's provider's. The outputs are identifiers only: no
IP address, no key material, no policy text.

**The key pair is referenced, never managed.** Its private key belongs in the
password manager. A key generated by Terraform would be stored in the state,
and an `aws_key_pair` resource needs the public key in its configuration,
which AWS does not return on import.

## A copy of your own

```bash
cd standby/terraform/examples/basic
cp terraform.tfvars.example terraform.tfvars   # git-ignored: put your values here
terraform init
terraform apply -var instance_state=running    # first time only, so the bootstrap can finish
# ssh in; wait for BOOTSTRAP-OK at the end of /var/log/standby-bootstrap.log
terraform apply                                # the default: stopped
```

Then attach `uploader_policy_arn` to the identity the home uploader uses. That
identity and its access key stay out of Terraform, because a key created here
would be stored in the state.

- **Rebuilding is deliberate, never a side effect.** The AMI lookup moves
  every time Debian publishes an image, and a new AMI would replace the
  instance and lose the data staged on it. A user-data change would stop and
  start it for a script that only runs on the first boot. So the instance
  ignores both. To rebuild on the newest image:
  `terraform apply -replace=module.standby.aws_instance.standby`.
- **Tearing down.** The bucket has `prevent_destroy`, and Object Lock keeps
  every locked version until its retention ends. A practice copy needs
  `GOVERNANCE`, a short retention, and the `prevent_destroy` line removed from
  `offsite.tf` before `terraform destroy`.

## Code public, values private

This repository holds the code: the module, an example root with placeholder
values, and the tests. A private repository holds the values. It calls the
module pinned to a release tag:

```hcl
module "standby" {
  source = "git::https://github.com/ValentinTorassa/homelab-blueprint.git//standby/terraform?ref=vX.Y.Z"

  create_bucket          = false # the bucket exists; it stays outside Terraform
  create_uploader_policy = false
  bucket_name            = var.bucket_name
  subnet_id              = var.subnet_id
  key_name               = var.key_name
  ssh_ingress_cidr       = var.ssh_ingress_cidr

  # Exactly what exists, or the import plans a replacement.
  security_group_name        = var.security_group_name
  security_group_description = var.security_group_description
  role_name                  = var.role_name
  read_policy_name           = var.read_policy_name
  instance_profile_name      = var.instance_profile_name
  instance_tags              = var.instance_tags
}
```

Terraform fetches the whole repository and reads the module from the
subdirectory, so the module's `../iam` and `../standby-userdata.sh` come with
it. The state lives with the values: it holds the account, the bucket, the
instance and the operator's address.

### Adopting what already exists

The standby built by `provision-standby.sh` is adopted, not rebuilt, with
`import` blocks in the private root. The IDs below are placeholders:

```hcl
import {
  to = module.standby.aws_security_group.standby
  id = "sg-0123456789abcdef0"
}
import {
  to = module.standby.aws_iam_role.standby
  id = "standby-role"
}
import {
  to = module.standby.aws_iam_role_policy.standby_read
  id = "standby-role:standby-read-backups" # role:policy
}
import {
  to = module.standby.aws_iam_role_policies_exclusive.standby
  id = "standby-role"
}
import {
  to = module.standby.aws_iam_role_policy_attachments_exclusive.standby
  id = "standby-role"
}
import {
  to = module.standby.aws_iam_instance_profile.standby
  id = "standby-profile"
}
import {
  to = module.standby.aws_instance.standby
  id = "i-0123456789abcdef0"
}
import {
  to = module.standby.aws_ec2_instance_state.standby
  id = "i-0123456789abcdef0"
}
```

With `create_bucket = true`, the bucket resources are counted, so their
addresses end in `[0]` (`module.standby.aws_s3_bucket.offsite[0]`, imported by
bucket name), and so does `aws_iam_policy.uploader[0]` (imported by ARN).

The first `terraform plan` should report only imports: "N to import, 0 to
add, 0 to change, 0 to destroy". Any other line is a difference between the
account and the code. Settle each one before the first apply, by fixing an
input or by accepting the change on purpose. A line that says "must be
replaced" means stop.

### Drift: a weekly plan, on the server

Once adopted, the private repository runs this every week on the server, as
a job wrapped in [`run-and-report.sh`](../../monitoring/jobs/run-and-report.sh):

```bash
terraform plan -detailed-exitcode -input=false -no-color
```

| Exit | Meaning | Gatus |
|---|---|---|
| 0 | the account matches the code | green |
| 2 | drift: a port opened by hand, a policy added to the role, the standby left running, a resting type a drill did not put back | red |
| 1 | the plan **could not run** (credentials, network, state) | red, with a different message |

Terraform's codes are the reverse of [`check_drift.py`](../../drift/check_drift.py)'s
(there 1 is drift and 2 is "could not check"), so the job's wrapper says which
one it was in the line it sends. The plan runs with a read-only identity: it
only needs to describe the account. Changing the account stays a person
running `apply`.

It never runs in this repository's CI. A plan needs credentials for the real
account and the real state, and a public workflow is the wrong place for
either. Its logs are readable by anyone, and its output would print the
identifiers this repository leaves out. Public CI checks the code against its
tests. The server checks the account against the code.

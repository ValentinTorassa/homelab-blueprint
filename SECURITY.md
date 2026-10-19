# Security and scope

## What this repository is

A **sanitized blueprint** of how a private homelab is operated. It is not a
copy of the live configuration:

- Every hostname, domain, IP address, VPN name, cloud account, resource ID,
  bucket name and service name is a placeholder: `example.com`,
  `*.example.internal`, `192.0.2.0/24` (TEST-NET-1), `111122223333`,
  `i-0123456789abcdef0`, `example-offsite-bucket`, `app-a`, `blog`, `mailer`.
- The scripts were rewritten to be generic and self-contained. They keep the
  logic and drop anything that identifies the real environment.
- No credentials, tokens, keys or webhook URLs are included. Files that hold
  them in the real setup appear here only as `*.example` templates with
  `change-me` values.

## Running it

- The tests and `make demo` run unprivileged, in temporary directories, with
  no network access and no cloud calls.
- `backups/restore_test.py` and `backups/backup_snapshot.sh` are meant to run
  as root on a real server. Read them first. Run them against a test backup
  root before pointing them at production data.
- `standby/*.sh` call AWS and create billable resources. `takeover-drill.sh
  --dry-run` prints every command and executes none. Start there.

## Handling secrets in your own copy

- Per-endpoint Gatus tokens and the GitHub read-only token live in 0600 files
  outside git (`~/.config/homelab/`). Never put them in the YAML.
- Use least-privilege identities: a read-only GitHub token for the drift
  check, a no-delete uploader for backups, and a read-only instance role for
  the standby (see `standby/iam/`).
- Keep the secrets that recovery depends on outside the system being
  recovered.
- `.gitignore` is not a security boundary. Review `git diff --cached` before
  every push, and run a secret scanner such as `gitleaks`.

## Reporting

If you find something in this repository that looks like a real secret or a
real identifier, please open an issue without quoting it, or contact the
maintainer through GitHub. It will be treated as an incident: removed,
rotated and noted.

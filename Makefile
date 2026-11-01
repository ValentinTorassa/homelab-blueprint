PYTHON ?= python3
SHELL_SCRIPTS := $(shell git ls-files --cached --others --exclude-standard '*.sh')

.PHONY: test test-docker check lint demo diagrams terraform all

all: lint check test

test:
	$(PYTHON) -m unittest discover -s tests

# The mailer family against a real Postgres: needs docker and the image pulled.
test-docker:
	$(PYTHON) -m unittest discover -s tests -p test_restore_docker.py -v

check:
	$(PYTHON) monitoring/check_gatus_config.py
	$(PYTHON) backups/check_backup_consistency.py

lint:
	@for f in $(SHELL_SCRIPTS); do bash -n "$$f" || exit 1; done
	@if command -v shellcheck >/dev/null; then shellcheck $(SHELL_SCRIPTS); \
	 else echo "shellcheck not installed: syntax check only"; fi

demo:
	bash backups/demo.sh

# Redraw the README diagrams from docs/diagrams/render.py (demo.svg: render.py --demo).
diagrams:
	$(PYTHON) docs/diagrams/render.py

# The standby as Terraform: fmt, validate and the offline tests (mock provider).
# init downloads the AWS provider from the registry, and nothing else touches
# the network. No command here can reach an AWS account: every AWS_* variable
# is dropped, and the profile and credential files point nowhere.
TF_DIR := standby/terraform
TF_EXAMPLE := $(TF_DIR)/examples/basic
TF := env $(foreach v,$(filter AWS_%,$(.VARIABLES)),-u $(v)) \
	AWS_PROFILE=nonexistent-profile AWS_CONFIG_FILE=/dev/null \
	AWS_SHARED_CREDENTIALS_FILE=/dev/null AWS_EC2_METADATA_DISABLED=true \
	CHECKPOINT_DISABLE=1 terraform

terraform:
	$(TF) fmt -check -recursive $(TF_DIR)
	$(TF) -chdir=$(TF_DIR) init -backend=false -input=false -lockfile=readonly
	$(TF) -chdir=$(TF_DIR) validate
	$(TF) -chdir=$(TF_DIR) test
	$(TF) -chdir=$(TF_EXAMPLE) init -backend=false -input=false -lockfile=readonly
	$(TF) -chdir=$(TF_EXAMPLE) validate
	$(TF) -chdir=$(TF_EXAMPLE) test -var-file=terraform.tfvars.example

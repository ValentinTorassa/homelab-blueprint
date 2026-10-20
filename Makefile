PYTHON ?= python3
SHELL_SCRIPTS := $(shell git ls-files --cached --others --exclude-standard '*.sh')

.PHONY: test test-docker check lint demo all

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

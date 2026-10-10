.DEFAULT_GOAL := help
SHELL := /bin/bash
.PHONY: help install lint unit integration test build artifact-check browser-install e2e vuln check dev stop clean
help:
	@printf '%s\n' 'make install: pinned tools and dependencies' 'make lint / unit / integration: separate verification layers' 'make test: unit + integration' 'make check: all applicable checks'
test: unit integration
build artifact-check browser-install e2e vuln dev stop:
	@echo '$@: inapplicable: composite action library; no application, browser or shipping image'
check: lint test
clean:
	rm -rf .artifacts

export PATH := $(CURDIR)/.artifacts/venv/bin:$(PATH)
install:
	mise trust .mise.toml
	mise install
	python3 -m venv .artifacts/venv
	.artifacts/venv/bin/python -m pip install -r requirements-tools.txt
lint:
	actionlint
	shellcheck scripts/*.sh activate/*.sh deploy/*.sh publish/*.sh preview/*.sh set-service-env/*.sh tests/*.sh
	python3 scripts/lint-actions.py
unit:
	python3 -m unittest discover -s tests -v
integration:
	@set -e; for suite in tests/*.test.sh; do bash "$$suite"; done
	bash scripts/test-parser-presence.sh

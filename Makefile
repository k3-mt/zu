# Zu — common dev tasks. `make help` lists them.
# Tests use the zu-testing pytest plugin: the default run is fast and hermetic
# (no network, no Docker); live/docker lanes are explicit opt-ins.

.PHONY: help sync test test-live test-docker test-all cov lint type check

help:  ## List targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  %-12s %s\n", $$1, $$2}'

sync:  ## Install the whole workspace + dev tools (editable)
	uv sync

test:  ## Fast, hermetic unit tests (no network, no Docker) — the default lane
	uv run pytest

test-live:  ## + tests marked @live (need network / real model + keys)
	uv run pytest --run-live

test-docker:  ## + tests marked @docker (need a real Docker daemon + the zu image)
	uv run pytest --run-docker

test-all:  ## Everything: unit + live + docker
	uv run pytest --run-live --run-docker

cov:  ## Unit tests with the coverage gate ([tool.coverage] fail_under)
	uv run pytest --cov

lint:  ## ruff
	uv run ruff check packages

type:  ## mypy
	uv run mypy packages

check: lint type cov  ## The full CI gate, locally

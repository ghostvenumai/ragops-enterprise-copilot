# Troubleshooting

## Python Command Blocked By Sandbox

Some host sandbox runs may fail with a `bwrap` loopback error. Retry the command
or use an approved repository-scoped execution when the sandbox wrapper itself
is the failing component.

## Missing Quality Tools

Run `make setup` to install the pinned development dependencies in `.venv`.
Missing Ruff, MyPy, Bandit, pip-audit, pytest-cov, or SBOM tooling is recorded as
`not_executed` and makes `make verify` fail.

## Docker Compose Command

The Makefile selects classic `docker-compose` when installed and otherwise uses
`docker compose`. Override explicitly with `COMPOSE="docker compose"` when
needed.

## BuildKit DNS

This host's BuildKit sandbox could not resolve PyPI while standard isolated
containers could. The Makefile defaults to `DOCKER_BUILDKIT=0`; environments
with working BuildKit can run `DOCKER_BUILDKIT=1 make build`.

## Docker Runtime

The demo exposes only API port 8000 and dashboard port 8501. PostgreSQL and
Qdrant stay on the internal data network. Check service health with
`docker-compose --profile demo ps`.

.PHONY: setup build up down test lint typecheck security evaluate verify demo evidence clean loop loop-status loop-resume

PYTHON ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)
export PATH := $(CURDIR)/.venv/bin:$(PATH)
COMPOSE ?= $(if $(shell command -v docker-compose 2>/dev/null),docker-compose,docker compose)
DOCKER_BUILDKIT := 0
COMPOSE_DOCKER_CLI_BUILD := 0
export DOCKER_BUILDKIT COMPOSE_DOCKER_CLI_BUILD

setup:
	python3 -m venv .venv
	.venv/bin/python -m pip install --upgrade pip==26.2 setuptools==83.0.0 wheel==0.47.0
	.venv/bin/python -m pip install -e ".[dev]" -c constraints.txt

build:
	$(COMPOSE) --profile demo build

up:
	$(COMPOSE) --profile demo up --build

down:
	$(COMPOSE) down

test:
	pytest

lint:
	$(PYTHON) scripts/verify.py --only lint

typecheck:
	$(PYTHON) scripts/verify.py --only typecheck

security:
	$(PYTHON) scripts/verify.py --only security

evaluate:
	$(PYTHON) scripts/run_evaluation.py

verify:
	$(PYTHON) scripts/verify.py

demo:
	$(PYTHON) scripts/run_demo.py

evidence:
	$(PYTHON) scripts/build_evidence_bundle.py

clean:
	$(PYTHON) scripts/safe_clean.py

loop:
	./scripts/autonomous_build.sh

loop-status:
	$(PYTHON) loop/controller.py status

loop-resume:
	$(PYTHON) loop/controller.py run

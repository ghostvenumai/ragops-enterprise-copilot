.PHONY: setup build up down test lint typecheck security evaluate verify demo evidence clean loop loop-status loop-resume master-loop master-loop-dry-run master-loop-resume video video-dry-run test-redis-integration test-qdrant-integration phase4-verify phase5-verify

PYTHON ?= $(if $(wildcard .venv/bin/python),.venv/bin/python,python3)
export PATH := $(CURDIR)/.venv/bin:$(PATH)
COMPOSE ?= $(if $(shell command -v docker-compose 2>/dev/null),docker-compose,docker compose)
DOCKER_BUILDKIT := 0
COMPOSE_DOCKER_CLI_BUILD := 0
export DOCKER_BUILDKIT COMPOSE_DOCKER_CLI_BUILD

setup:
	python3 -m venv .venv
	.venv/bin/python -m pip install --upgrade pip==26.2 setuptools==83.0.0 wheel==0.47.0
	.venv/bin/python -m pip install -e ".[dev,persistence,identity,vector]" -c constraints.txt

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

master-loop:
	./run_loop.sh

master-loop-dry-run:
	./run_loop.sh --dry-run

master-loop-resume:
	./run_loop.sh --resume

video:
	./video/build_demo.sh

video-dry-run:
	./video/build_demo.sh --dry-run

.PHONY: migrate migration-check product-verify
migrate:
	$(PYTHON) -m alembic upgrade head

migration-check:
	$(PYTHON) -m alembic check

product-verify:
	$(PYTHON) scripts/product_verify.py

test-redis-integration:
	$(PYTHON) -m pytest tests/integration/test_redis.py -q

phase4-verify:
	$(PYTHON) scripts/phase4_verify.py

test-qdrant-integration:
	$(PYTHON) scripts/qdrant_live_gate.py

phase5-verify:
	$(PYTHON) scripts/phase5_verify.py

phase6-verify:
	$(PYTHON) scripts/phase6_verify.py

phase7-verify:
	$(PYTHON) scripts/phase7_verify.py

phase8-verify:
	$(PYTHON) scripts/phase8_verify.py

phase9-verify:
	$(PYTHON) scripts/phase9_verify.py

rc-gate:
	$(PYTHON) scripts/rc_gate.py

rc-live-gate:
	$(PYTHON) scripts/rc_live_gate.py

e2e-setup:
	.venv/bin/python -m pip install -e ".[e2e]" -c constraints.txt
	.venv/bin/python -m playwright install chromium

oidc-integration-up:
	docker compose -f docker-compose.integration.yml up -d

oidc-integration-bootstrap:
	$(PYTHON) scripts/configure_local_oidc_integration.py

oidc-integration-check:
	$(PYTHON) scripts/oidc_integration_check.py

test-dr-integration:
	@echo "NOT_EXECUTED: requires authorized PostgreSQL/Qdrant deployment"

test-postgres-integration:
	$(PYTHON) -m pytest tests/product/test_postgres.py -q

test-docker-integration:
	@echo "NOT_EXECUTED: Docker daemon unavailable in sandbox"

production-check:
	$(PYTHON) scripts/production_check.py

backup:
	@echo "Use scripts/operator_backup.py with an explicit source and destination"

backup-verify:
	$(PYTHON) -c "from pathlib import Path; from ragops.ops.backup import verify_backup; print(verify_backup(Path('$(BACKUP)')))"

test-openai-integration:
	@echo "NOT_EXECUTED: requires explicit credentials and network access"

test-azure-openai-integration:
	@echo "NOT_EXECUTED: requires explicit credentials and network access"

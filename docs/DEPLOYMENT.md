# Deployment

## Local Demo

~~~bash
make setup
make verify
make build
make up
~~~

Open the copilot at <http://localhost:8501> and API documentation at
<http://localhost:8000/docs>. The default deterministic provider requires no
secrets.

Run **make down** to stop services.

## Build Profiles

**make build** activates the Compose demo profile so profiled API and dashboard
services are actually included in the image build. The Makefile defaults to the
classic isolated builder on this host because its BuildKit DNS namespace cannot
resolve PyPI. Override on a working environment with:

~~~bash
make DOCKER_BUILDKIT=1 COMPOSE_DOCKER_CLI_BUILD=1 build
~~~

Neither path uses host networking.

## Dashboard Configuration

The dashboard receives RAGOPS_API_URL=http://api:8000 through Compose and
reaches FastAPI on appnet. PostgreSQL and Qdrant have no host ports.

## Docker Security Baseline

- Non-root API and dashboard runtime user.
- No privileged containers.
- No host networking.
- No Docker socket mount.
- no-new-privileges.
- Linux capabilities dropped; PostgreSQL receives only required init capabilities.
- CPU, memory, and process limits.
- Healthchecks for all services.
- Internal network for data services.
- Read-only root filesystems where practical.
- tmpfs for temporary writes.

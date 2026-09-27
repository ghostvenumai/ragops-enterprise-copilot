# Async operations

Set `RAGOPS_REDIS_URL`, `RAGOPS_QUEUE_NAME`, `RAGOPS_WORKER_CONCURRENCY` and
`RAGOPS_JOB_MAX_ATTEMPTS`. If `RAGOPS_ASYNC_INGESTION_REQUIRED=true`, startup
fails closed when the Redis URL is absent or invalid. Redis is internal-only,
memory bounded and not a secret store. Protect it with network isolation and
provider authentication when crossing trust boundaries.

Queue depth, active/completed/failed jobs, retries and stage failures should be
exported without document IDs as metric labels. Detailed diagnostics use the
job correlation ID and never include document contents or secrets.

## Host-side RC Redis gate

The selected `redis_worker` live gate uses the separate `rc-redis` service in
`docker-compose.rc.yml` when `RAGOPS_REDIS_URL` points at the Docker-only host
`redis`. It runs as the image's non-root `redis` user, with a read-only root,
`no-new-privileges`, no Linux capabilities, and ephemeral `/tmp` data. The
service publishes container port 6379 on a Docker-assigned port bound only to
`127.0.0.1`; the gate queries the exact Compose project's `rc-redis` service to
discover that port. It disables RDB/AOF persistence because the gate uses disposable synthetic data. Production
Redis configuration and volumes are not changed by this override.

For a local checkout, run `make setup`. The pinned Python `redis` client is a
core package dependency for the worker runtime, so the host integration gate
does not need an undocumented manual `pip install redis` step.

## Host-side RC Qdrant gate

Run `make test-qdrant-integration` on the authorized host. The setup installs
the pinned `vector` extra (`qdrant-client==1.15.1`). The gate creates a unique
Compose project and synthetic collection, exposes REST only on a Docker-picked
loopback port, and addresses only its exact `rc-qdrant` service. Its image is
Qdrant `v1.15.1`; the data directory is an ephemeral, memory-backed `tmpfs`
with a 256 MiB cap, while the container has 512 MiB memory, one CPU, 128 PIDs,
a read-only root, no added capabilities, and `no-new-privileges`. No gRPC port
or persistent volume is needed for this disposable verification. The gate
reports service/data cleanup separately and writes sanitized details to
`evidence/product-v1/rc-live/qdrant.json`. Docker daemon/network absence is
BLOCKED; a started service or executed contract scenario that fails is FAIL.

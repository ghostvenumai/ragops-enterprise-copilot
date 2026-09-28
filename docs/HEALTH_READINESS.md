# Health and readiness

`/health` is process liveness. It performs no dependency probes and keeps answering `200`
while PostgreSQL, Redis or Qdrant are down, so an orchestrator never restarts a healthy
process because a dependency failed.

`/ready` is the traffic gate. It runs one bounded probe per dependency in parallel
(`SELECT 1`, Redis `PING`, Qdrant `collection_exists`) under a shared deadline of
`RAGOPS_DEPENDENCY_TIMEOUT_SECONDS` (default 2 s) and answers:

| Status | Meaning |
| --- | --- |
| `200 ready` | every critical dependency answered |
| `503 unready` | a critical dependency is down or timed out |
| `503 shutting_down` | shutdown started; the instance no longer takes traffic |

```json
{"status": "unready",
 "components": {"postgres": {"critical": true, "status": "down", "reason": "timeout"},
                "redis": {"critical": true, "status": "up", "reason": "ok"},
                "qdrant": {"critical": false, "status": "not_configured", "reason": "not_configured"}},
 "documents": 42}
```

Reasons are stable codes only (`ok`, `unreachable`, `timeout`, `error`, `not_configured`);
URLs, hosts, credentials and exception messages never appear. Criticality follows the
configuration: PostgreSQL when `RAGOPS_DATABASE_URL` is set, Redis with the redis
rate-limit backend or required async ingestion, Qdrant with `RAGOPS_VECTOR_PROVIDER=qdrant`.
An unconfigured dependency is reported but never blocks readiness. `documents` is the
aggregate demo-corpus size kept for the dashboard.

Startup never probes, so an instance may start while dependencies are down; it stays
unready and turns ready without a restart once they recover. On shutdown the instance turns
unready first, then closes the database pool, the Qdrant client and the Redis clients.

Inside a request, an unavailable database or Redis maps to a detail-free
`503 {"detail": "dependency unavailable"}` with `Retry-After: 1`; the rate limiter fails
closed. `/metrics` stays available and reports `ragops_ingestion_queue_up 0` instead of
failing when the queue is down.

All clients are bounded: PostgreSQL `connect_timeout`, `tcp_user_timeout` and
`statement_timeout`; Redis socket timeouts with client-side retries disabled (the caller
decides, which avoids retry amplification); Qdrant request timeouts. The
`readiness_failure_recovery` RC gate proves this against live dependencies.

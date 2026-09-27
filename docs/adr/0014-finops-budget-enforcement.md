# ADR 0014: tenant budget enforcement

Usage records remain the source of truth. Preflight computes projected spend before provider execution and returns ALLOW, ALLOW_WITH_WARNING, ROUTE_CHEAPER or DENY_BUDGET_LIMIT. SQLite tests cover deterministic policy; strict concurrent reservation requires PostgreSQL locking and remains an external gate.

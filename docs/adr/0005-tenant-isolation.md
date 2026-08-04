# ADR 0005: Tenant Isolation

## Decision

Apply tenant and RBAC filters before scoring retrieval results.

## Rationale

Pre-score filtering prevents cross-tenant data from influencing rank, evidence,
or telemetry.

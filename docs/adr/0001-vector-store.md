# ADR 0001: Vector Store

## Decision

Use Qdrant as the target vector store for Docker/demo architecture and a
deterministic in-memory vector index for local tests.

## Rationale

Qdrant is operationally simple for demos and supports metadata filtering. The
local index keeps tests reproducible without external services.

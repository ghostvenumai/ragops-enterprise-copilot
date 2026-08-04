# ADR 0003: Hybrid Retrieval

## Decision

Combine BM25-style lexical scoring with deterministic hashed-vector similarity
and reranking.

## Rationale

This gives reproducible local behavior while preserving the enterprise retrieval
shape expected by production RAG systems.

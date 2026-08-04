# ADR 0007: Audit Logging

## Decision

Write local JSONL audit events with correlation IDs and redacted details.

## Rationale

JSONL is inspectable, append-friendly, and easy to replace with a production log
sink later.

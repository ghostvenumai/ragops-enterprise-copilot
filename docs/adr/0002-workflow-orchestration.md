# ADR 0002: Workflow Orchestration

## Decision

Use a typed custom state machine instead of a heavy orchestration dependency.

## Rationale

The workflow is small, transparent, bounded, and easy to test. LangGraph can be
introduced later behind the same state boundaries.

# ADR 0009: Codex Loop Security Model

## Decision

The autonomous loop invokes Codex through a fixed noninteractive command and
runs only allowlisted local quality commands afterward.

## Rationale

The controller must not execute arbitrary model text. State is written
atomically to support recovery and review.

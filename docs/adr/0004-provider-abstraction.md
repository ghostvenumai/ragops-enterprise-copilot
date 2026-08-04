# ADR 0004: Provider Abstraction

## Decision

Implement `DeterministicTestProvider`, `OpenAIProvider`, and
`AzureOpenAIProvider` behind a protocol.

## Rationale

Tests must run without secrets or cost. External providers remain opt-in through
environment variables.

# ADR 0006: Deterministic Test Provider

## Decision

Use a deterministic local provider as the default.

## Rationale

It enables reproducible tests, stable evidence, no external cost, and no secret
requirement.

## German Demo Responses

The portfolio UI uses German as its default response language. The provider maps
approved synthetic source titles to controlled German summaries and keeps source
identifiers unchanged. Unknown sources receive a German evidence-only fallback
instead of raw chunk output. This preserves deterministic behavior and prevents
unreviewed document wording from becoming answer text.

The mapping is deliberately corpus-bound. Changes to synthetic source facts must
update the mapping and regression tests in the same change.

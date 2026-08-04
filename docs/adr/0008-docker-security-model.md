# ADR 0008: Docker Security Model

## Decision

Use non-root containers, read-only filesystems, dropped capabilities,
`no-new-privileges`, resource limits, healthchecks, and internal networks.

## Rationale

The demo should model secure defaults without privileged host access.

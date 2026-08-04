# Assumptions

- `/home/serverserver` was not a Git repository at start. The project was
  created in `/home/serverserver/ragops-enterprise-copilot` and initialized as a
  dedicated repository.
- All data in `data/` is synthetic and must remain synthetic.
- The default LLM provider is deterministic and local. OpenAI and Azure OpenAI
  providers are opt-in through environment variables.
- The portfolio dashboard and deterministic default answers use German. Stable
  source titles and source IDs remain unchanged for evaluation and audit evidence.
- Local PDF and DOCX support uses text-layer extraction by UTF-8 or Latin-1
  decoding for synthetic fixtures. Production-grade binary parsing is a planned
  adapter extension.
- PostgreSQL and Qdrant are deployed as hardened Docker services. Core tests
  remain file-backed and deterministic, so external services are not required
  for the default test provider.
- Some sandbox helper operations intermittently failed with
  `bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted`. Approved
  alternatives were limited to repository-local edits and fixed commands; the
  incident history is tracked in `.loop/blockers.json`.
- BuildKit DNS resolution failed on this host while isolated runtime containers
  had normal network access. The verified fallback uses the classic Docker
  builder on Docker's standard bridge network, without host networking.
- On 2026-08-03 the demo profile was built and started successfully. API,
  dashboard, PostgreSQL, and Qdrant passed their configured health checks.

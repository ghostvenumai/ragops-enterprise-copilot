# Assumptions

- `/home/serverserver` was not a Git repository at start. The project was
  created in `/home/serverserver/tools/ragops-enterprise-copilot` and initialized as a
  dedicated repository.
- All data in `data/` is synthetic and must remain synthetic.
- The default LLM provider is deterministic and local. OpenAI and Azure OpenAI
  providers are opt-in through environment variables.
- The portfolio dashboard and deterministic default answers use German. Stable
  source titles and source IDs remain unchanged for evaluation and audit evidence.
- Local PDF and DOCX ingestion uses real binary format parsers (`pypdf`,
  `python-docx`) that extract the text layer/paragraphs from actual PDF and
  OOXML `.docx` fixtures. Plain-text-with-extension fixtures are no longer
  used for these two suffixes.
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
- The autonomous media build uses installed FFmpeg, FFprobe, and headless Google
  Chrome. Xvfb is not required because capture is browser-headless and contains
  no desktop-coordinate automation.
- OpenAI text-to-speech is optional and reads its credential only from
  `OPENAI_API_KEY`. A complete validated local cache works without that
  credential. A cache miss without it reports `READY_EXCEPT_EXTERNAL_BLOCKER`
  and creates no replacement audio; silence is available only through the
  explicit `--skip-tts` preview mode.
- `MASTER_BRIEFING.md` describes a generic Python data processor. In this
  repository, the existing RAGOps application is the real processor: ingestion,
  validation, retrieval, security, export, and demo actions execute production
  code paths rather than a separate video-only mock.

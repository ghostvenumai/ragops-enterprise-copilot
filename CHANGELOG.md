# Changelog

## 0.2.1 - 2026-08-04

- Localized the complete Streamlit user interface to German.
- Kept established RAG, LLM, Retrieval, Recall, and Precision terminology.
- Updated UI tests, demo instructions, and visual validation evidence.
- Added source-bound German response composition for deterministic RAG answers.
- Added German CRM evidence and regression coverage against raw English chunk output.
- Expanded the synthetic knowledge base from 11 to 20 documents across three tenants.
- Added Unicode-aware lexical evidence gating and controlled no-evidence refusal.
- Restricted API citations to sources actually used by the composed answer.
- Expanded the executable gold dataset from 25 to 28 cases.

## 0.2.0 - 2026-08-03

- Replaced the static evidence dashboard with a modern chat-first copilot workspace.
- Added cited answers, response telemetry, prompt suggestions, and session reset.
- Added tenant-filtered knowledge-base, observability, and governance views.
- Connected Streamlit to FastAPI over the Compose application network.
- Changed evaluation and audit endpoints to return structured dashboard data.
- Added dashboard client tests and visual desktop/mobile validation evidence.
- Fixed make build so Compose-profiled services are included.
- Made the classic isolated Docker builder the reproducible host default.
- Documented GUI architecture, identity boundaries, operation, and demo flow.

## 0.1.0

- Initial portfolio project foundation.
- Added controlled autonomous loop design.
- Added deterministic local RAG architecture.

---
document_id: DOC-ALPHA-SECURITY-OVERVIEW
tenant_id: tenant-alpha
title: Copilot Security Overview
classification: security
access_level: internal
version: v1
department: security
created_at: 2026-07-18
valid_from: 2026-07-18
---
Synthetic document. This overview explains how the copilot protects tenant data. Every request first passes the tenant guard, so answers only use documents and CRM records of the requesting tenant. Role levels from viewer to admin decide which document sensitivity a user may read. Retrieved text is scanned for injection patterns; flagged chunks are excluded from answer composition. Personal data in outputs is masked, every factual answer must carry citations, and the copilot declines when evidence is insufficient. Uploads are restricted to an allowlist of file types with size limits and safe file names.

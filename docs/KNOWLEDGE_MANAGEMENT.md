# Enterprise Knowledge Management (Phase 3)

Phase 3 adds tenant-owned workspaces, collections, documents and immutable
document versions. `KnowledgeManagementService` owns lifecycle and duplicate
rules; SQLAlchemy repositories remain tenant-scoped and return the same opaque
not-found result for another tenant.

Documents use explicit states: `uploaded -> validating -> ready_for_ingestion ->
processing -> indexed`; failures may return to validation, and inactive or
superseded records are retained for audit. Replacement uploads create a new
version and never overwrite bytes or history. Retrieval should use the current
version only unless an explicit historical query is authorized.

`LocalDocumentBlobStore` stores generated hash-based keys below a configured
data root. Filenames are normalized and allowlisted (`pdf`, `docx`, `md`, `txt`,
`csv`), MIME, size, empty content, traversal and duplicate hashes are checked.
S3, Azure Blob and MinIO can implement the same `DocumentBlobStore` interface.

The service requires an authenticated tenant and actor. API request tenant
fields are never used to select a repository tenant in OIDC mode. Collection and
document access must be checked before retrieval, with the existing RBAC access
levels retained.

Uploads are validated at intake: allowed extension and MIME type, non-empty, at most
2 MB, and binary formats must carry their signature (`%PDF-` for PDF, a ZIP header
for DOCX), so a renamed or corrupted file is rejected with
`file content does not match its type` before any job exists.
`GET /v1/ingestion/jobs` returns each job with its document title and file name for
the caller's tenant only.

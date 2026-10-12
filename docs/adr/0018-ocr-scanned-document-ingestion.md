# ADR 0018: Page-level OCR for scanned PDFs and images

Status: accepted (ENT-11.7).

Until ENT-11.7 the ingestion worker read PDF text with pypdf only. A scanned PDF has no text
layer, so it produced no chunks (`empty_document`), and JPEG/PNG uploads were rejected at
intake. Scanned letters are the main source material of the follow-up milestone ENT-11.8, so
they must be read locally, with exact source pages and positions, without a second ingestion
architecture and without weakening tenant isolation or the citation contract.

## Decision

**Engine.** Tesseract OCR 5 (`tesseract` CLI, LSTM engine, languages `deu+eng`) runs locally
as a subprocess. PDF pages are rasterized with poppler's `pdftoppm`. Both are CPU-only, need
no network and no GPU, and are packaged by the distribution (`tesseract-ocr`,
`tesseract-ocr-deu`, `tesseract-ocr-eng`, `poppler-utils`). There is no cloud or paid OCR
fallback. Availability is never inferred from a Python import: the worker probes the binary
(`tesseract --version`) and the installed language data (`--list-langs`). A missing engine or
language fails the job with `ocr_engine_unavailable` / `ocr_language_unavailable`.

**Per-page routing.** Each PDF page is classified from evidence on that page, not from the
file name or metadata:

- the pypdf text layer and its plausibility (character and word statistics),
- whether the text is only drawn invisibly (text render mode 3, typical of OCR layers),
- the page area covered by image XObjects and inline images, computed by following the
  content stream's transformation matrix (bounded operation count, nested forms depth ≤ 3).

| Evidence | Class | Extraction |
| --- | --- | --- |
| usable visible text, image coverage < 15 % | `native_text` | text layer only, no OCR |
| usable visible text and images ≥ 15 % | `mixed_content` | text layer + OCR of the image regions only; OCR lines already in the text layer are dropped |
| no usable text (none, garbled or invisible-only) and images or vector drawing | `scanned_image` | full-page OCR |
| no text, no images, no drawing — or a blank render | `empty_page` | nothing |
| OCR failed, timed out or exceeded a work limit | `unreadable` | nothing indexed |
| OCR ran but failed the quality gate | `ocr_review_required` | nothing indexed |

JPEG and PNG are one virtual page 1 routed like `scanned_image`. DOCX, CSV, Markdown and TXT
keep their previous extraction unchanged.

**Coordinates.** OCR boxes are stored in `page-normalized-v1`: `[x0, y0, x1, y1]` in `[0, 1]`,
origin top-left of the page *as displayed* (PDF crop box after `/Rotate`; image after EXIF
orientation), x to the right, y downwards. Page width/height are stored with their unit
(`pt` for PDF, `px` for images). Preprocessing (EXIF orientation, grayscale, resolution
normalization, conditional autocontrast and median filter, 90° orientation correction,
small-angle deskew) is recorded per page; every geometric step is an affine transform whose
inverse maps OCR boxes back to the original page, so stored boxes refer to the uploaded
document, not to the processed image.

**Quality gate.** OCR confidence is Tesseract's per-word score, a tool-derived indicator and not
a probability of correctness. A page is `ocr_review_required` when no text was recognized on an
inked page, the mean word confidence is below 65, more than 35 % of words score below 40,
more than 35 % of tokens are implausible, the output repeats itself, or geometry is invalid.
Edge-truncated lines and low ink coverage are recorded as flags.

**Partial indexing policy.** Pages that passed (`native_text`, `scanned_image`,
`mixed_content`) are indexed; `ocr_review_required` and `unreadable` pages are never indexed.
A version with such pages completes with `ingestion_status = review_required`, the page report
names the affected pages, and the dashboard shows "Prüfung erforderlich" instead of "Bereit".
A version without any indexable chunk fails (`ocr_review_required` or `empty_document`).

**Provenance.** Two tenant-owned tables (migration `0009_ocr_extraction`):
`document_page_extractions` (one row per page: class, method, engine and version, config
version, language, dimensions, transformations, quality, flags and line boxes — no text) and
`document_chunk_sources` (per chunk: page, method, OCR line indices and their boxes). The
Qdrant payload gains small additive fields (`extraction_method`, `ocr_quality`,
`page_width`, `page_height`, `coordinate_space`, `source_regions`); mandatory fields, filters
and point identifiers are unchanged. Chunks never span pages, so every chunk has exactly the
page number of its source page. Two authenticated, tenant- and access-level-scoped endpoints
expose the page report and a chunk's regions.

**Limits.** Text formats keep the 2 MB limit. PDF, JPEG and PNG may be up to 12 MB (the upload
route's request limit is 12.6 MB, every other route keeps 3 MB): a 300 dpi grayscale scan is
about 0.3–0.6 MB per page, so 12 MB holds a realistic letter bundle. Images: ≤ 40 megapixels,
≤ 20 000 px per side, a single frame, modes 1/L/LA/P/RGB/RGBA/CMYK. Rendering: ≤ 300 dpi and
≤ 12 megapixels per page, ≤ 400 megapixels per document. OCR: ≤ 40 pages, 60 s per page,
600 s per document. Every subprocess runs with a private 0700 temporary directory, a minimal
environment, a wall-clock timeout and resource limits (address space 1.5 GiB, CPU time,
file size 64 MB, 64 open files, no core dumps); `OMP_THREAD_LIMIT=1` keeps Tesseract on one
core. This is resource isolation, not a sandbox: the processes run as the worker user.

## Consequences

- The worker image installs `tesseract-ocr`, `tesseract-ocr-deu`, `tesseract-ocr-eng` and
  `poppler-utils`.
- OCR runs inside the job's processing transaction like the rest of extraction; the job list
  therefore shows the job as queued until it finishes. Live page progress is deferred.
- An interactive highlighting viewer is deferred; the stored coordinates support it.
- ENT-11.8 (classification and entity extraction) builds on the page report and chunk
  provenance and must not treat OCR confidence as evidence of factual correctness.

## Technical debt: retrieval quality of `deterministic-hash-v1`

The local `deterministic-hash-v1` embedding is a hashed bag of words in 64 dimensions. It is
reproducible and offline, but not semantic: unrelated words share buckets, and a single
short token (for example a reference number) can move a short chunk past a relevant one.
The first `ocr_ingestion` run with questions that contained the per-run reference number
cited the wrong page by chance (an offline simulation hit the target in 77.5 % and 2.75 %
of runs). The gate therefore uses fixed questions without those numbers and asserts the
ranking and a score margin on the stored vectors before it queries; it does not prove
semantic search quality. Real semantic embeddings, recall and ranking metrics belong to the
later semantic-embedding / search-quality milestone, which must replace this limitation
rather than tune fixtures around it.

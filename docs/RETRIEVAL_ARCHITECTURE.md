# Retrieval architecture

Authenticated identity is converted into an `AuthorizedVectorScope` before
search. A mandatory Qdrant filter is applied inside the vector query for tenant,
authorized workspace/collections, RBAC access level, indexed document/version
state and validity windows. Python-side checks are defense in depth, never the
primary tenant boundary.

The existing lexical BM25, deterministic/vector scoring, fusion, reranking and
citation validation remain intact. Historical or superseded versions are not
eligible for normal vector search. A future historical feature must supply an
explicitly authorized scope.

## Productive vector retriever (ENT-11.1)

`ragops.retrieval.vector_retriever.VectorRetriever` is the productive retrieval
boundary. Its only inputs are the verified `AuthenticatedUserContext`, the query
and `top_k` (1 to 20); there is no parameter through which a request body, query
string or header could name a tenant. `authoritative_scope` turns the context into
the immutable `AuthorizedVectorScope` and rejects anything that is not a verified
identity with a well-formed tenant and a known role before the index is called.

After the search every hit is checked again: a chunk of another tenant fails the
whole request (`TenantBoundaryViolation`) and raises the security signal
`vector_tenant_boundary_violation` without document text. A chunk embedded with
another or no embedding model, a query vector of another dimension, and a chunk
without text or citation metadata fail closed as well. Any index error becomes
`RetrievalUnavailable` (503). The module has no access to the local JSON corpus
or the hybrid demo retriever, which a static test enforces; no result is an empty
list, never a fallback. Results are ordered by score, document and chunk index.

## Query modes and the context builder (ENT-11.2)

`RAGOPS_QUERY_MODE` selects exactly one query path per process: `vector` (the default
with `RAGOPS_VECTOR_PROVIDER=qdrant`, required in production) or `demo` (the default
otherwise). There is no automatic switch between them.

In `vector` mode `/v1/query` runs `VectorQueryService`: the verified identity, the
`VectorRetriever`, `build_context`, the model router and query accounting (see
[FINOPS.md](FINOPS.md)). The demo workflow and
its local JSON corpus are not even constructed; the demo corpus endpoints
(`/v1/documents`, `/v1/documents/{id}`, `/v1/documents/ingest`) answer 404 and `/ready`
reports no demo document count. Request fields never select the tenant; with the
development identity provider the legacy body identity applies only in `demo` mode.

`build_context` orders hits by score, document and chunk index, drops chunks without text
or citation metadata, chunks with instruction-like text and duplicates (same `chunk_id`
or same `content_hash`), and then applies `top_k`, `RAGOPS_CONTEXT_MAX_CHUNKS` (default
20, the API ceiling) and `RAGOPS_CONTEXT_MAX_TOKENS` (default 6000). Tokens are estimated
as one per three UTF-8 bytes, which overestimates common tokenizers. A chunk that does
not fit is cut to the longest word prefix that fits and closes the context. Sources sit
in delimited blocks after a notice that they are untrusted reference data; text that
could close a block is neutralized. A foreign chunk fails the request.

Outcomes, audited as `query` events with `retrieval_outcome`:

| Situation | Response | Provider called |
| --- | --- | --- |
| Instruction-like question | 200, abstained, `blocked` | no |
| No chunk left after retrieval and context building | 200, abstained, `no_context` | no |
| Vector index unavailable | 503 `retrieval unavailable` | no |
| Foreign or incomplete chunk | 503 `retrieval integrity violation` (+ `vector_tenant_boundary_violation`) | no |
| Embedding model or dimension mismatch | 503 `retrieval misconfigured` | no |
| No eligible model for the tenant policy | 409 `no eligible model` | no |
| Budget hard limit | 429 `budget_limit_exceeded` | no |
| Accounting records or database missing | 503 `accounting not configured` | no |
| Accounting fails after the provider answered | 503 `accounting unavailable`, answer dropped | yes |
| Provider error | 503 `generation unavailable` | yes |
| Answer without source markers | 200, abstained, `ungrounded` | yes |

Citations of the vector path carry `document_id`, `document_version_id`, `chunk_id`
and `page_number` in addition to `source_id`, `title`, `tenant_id` and `score`; they
refer only to chunks in the context. The browser gate runs its API explicitly with
`RAGOPS_QUERY_MODE=demo` because its journeys ask questions of the demo corpus.

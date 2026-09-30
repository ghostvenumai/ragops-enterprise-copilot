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

`/v1/query` still answers through the demo workflow. It is switched to this
retriever together with the context builder (ENT-11.2), because the browser gate
runs the API against Qdrant and its query checks move with that change.

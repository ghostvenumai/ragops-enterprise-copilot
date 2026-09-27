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

# Security architecture

Vector queries require a non-empty trusted tenant and role. Client-supplied
tenant, collection or vector identifiers cannot broaden `AuthorizedVectorScope`.
Missing scope, malformed payload dates and unsupported access levels fail closed.
Cross-tenant IDs produce zero results or an opaque not-found response. Restricted
content is filtered at the vector-store query boundary according to existing
RBAC limits.

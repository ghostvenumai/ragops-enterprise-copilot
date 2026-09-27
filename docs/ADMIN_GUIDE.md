# Knowledge administration

Tenant administrators create workspaces and collections, set access levels and
allowed roles, inspect document versions, update metadata, deactivate records
and request reindexing. Every operation is tenant-scoped and should emit an
audit event with actor, tenant, resource, action, result, timestamp and
correlation ID. Deactivation is soft deletion; raw bytes and version history
remain available to authorized audit workflows.

The local store is for development and tests. Production deployments must point
the blob store at a dedicated external data service and must not place uploads
inside source-code directories.

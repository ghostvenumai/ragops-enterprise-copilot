# Backup and restore

`ragops.ops.backup` creates a versioned manifest and SHA-256 checksums in an atomic temporary directory before rename. Restore must be an explicit operator action after `verify_backup`; no autonomous loop invokes destructive restore. PostgreSQL and Qdrant snapshots are separate stores, so the manifest records a cutover timestamp and residual cross-store consistency risk.

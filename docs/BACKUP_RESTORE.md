# Backup and restore

`ragops.ops.backup` still copies a plain directory tree with a manifest and SHA-256
checksums. Coordinated, encrypted backups of the durable product state use
`ragops.ops.data_backup` and the operator CLI `scripts/operator_backup.py`
(`backup`, `verify`, `restore`).

Protected components: every Alembic-managed PostgreSQL table (logical export inside one
`REPEATABLE READ` snapshot; document bytes live in `document_versions`), the Qdrant
vector collection (vector size, distance, payload indexes and all points) and the document
blob store. Excluded: Redis, because queue messages are reconstructible from persisted
ingestion jobs and rate-limit counters are ephemeral; configuration, because non-secret
configuration is versioned and runtime secrets are never backed up.

Consistency: PostgreSQL is exported first inside its snapshot (`pg_current_snapshot` is
recorded), then Qdrant and the blobs; a barrier check requires every vector to reference a
document version inside the PostgreSQL snapshot, otherwise the backup fails as torn. The
manifest records `snapshot_at` and `completed_at`; writes after `snapshot_at` are not
captured. There is no point-in-time or incremental backup.

Artifact: one directory `backup-<uuid>` (mode 0700) with `manifest.json`, `postgres.bin`,
`qdrant.bin` and `blobs.bin`. Each component is canonical JSON encrypted with AES-256-GCM
(`cryptography` library, random 96-bit nonce); the associated data binds it to the backup
id and component name. The manifest lists sizes, ciphertext SHA-256, record counts and the
Alembic revision, and is authenticated with HMAC-SHA256. Encryption and MAC keys are HKDF
subkeys of one 256-bit key that lives outside the artifact in `RAGOPS_BACKUP_KEY_FILE`
(mode 0600); only a derived key id is stored. The directory appears atomically by rename.

Restore is an explicit operator action (`--confirm-target`). It verifies the signature,
format version, exact component set, sizes, hashes, authentication tags, record counts
and the barrier before any mutation, and it refuses system databases, the primary
database, non-empty databases, an existing Qdrant collection or alias, a non-empty blob
root and a different migration revision. It stages blobs and a Qdrant staging collection,
loads PostgreSQL in one transaction, then activates the blobs, creates the Qdrant alias and
commits PostgreSQL last; any failure rolls back and removes the staged state, so a failed
restore never activates. Restore is whole-backup only; tenant-scoped restore is not
supported. A second restore into the now non-empty target is rejected.

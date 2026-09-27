# Upgrade to the 0.2.0.dev0 foundation

This is a development snapshot, not a production release or release candidate.
The application still uses synthetic fixture persistence. No existing data was
converted or deleted. Existing demo API contracts and narration remain in place.

Changes that intentionally affect behavior:

- `admin` is tenant-scoped in RBAC and retrieval.
- Unknown provider names fail instead of choosing the deterministic provider.
- Unsupported environment values fail; accepted modes are local, development,
  test, demo and production. Production startup is deliberately blocked.
- File names containing Windows separators/drive syntax, control characters,
  leading/trailing whitespace or over 255 UTF-8 bytes are rejected.
- `make setup` installs the declared persistence extra for schema development.

The schema, repositories and migration/CI contracts are a pending foundation.
See DATABASE.md and MIGRATIONS.md; do not connect them to unauthenticated demo
endpoints. The current sandbox could not install the required packages or access
Docker. Those checks must be repeated in an authorized environment before phase
ENT-01 is complete. Later product phases remain pending in TASKS.yaml.

# ADR 0015: operational safety boundaries

Backups use manifest/checksum verification and atomic finalization. Rate limiting uses tenant/user identity rather than IP. Production Compose keeps data services on internal networks and places TLS/security headers at Caddy. Live cross-store restore consistency and RTO remain measured only in an authorized environment.

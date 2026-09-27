"""Synthetic OIDC issuer settings and signed test tokens for admin API security tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt

ISSUER = "https://issuer.synthetic.example/"
AUDIENCE = "ragops-api"


def bearer(private_key: str, tenant: str, roles: list[str]) -> dict[str, str]:
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": f"user-{tenant}",
        "tenant_id": tenant,
        "roles": roles,
        "exp": datetime.now(UTC) + timedelta(minutes=5),
    }
    return {"Authorization": f"Bearer {jwt.encode(claims, private_key, algorithm='RS256')}"}

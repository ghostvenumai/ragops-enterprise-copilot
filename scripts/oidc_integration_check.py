"""Validate a real Keycloak token without printing credentials or tokens."""
# ruff: noqa: E501, S310

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request

from ragops.auth.identity import AuthenticationError, OIDCIdentityProvider
from ragops.config.settings import Settings


def main() -> int:
    settings = Settings.from_env()
    settings.validate_identity_configuration()
    password = os.getenv("KEYCLOAK_TEST_USER_PASSWORD")
    if settings.identity_provider != "oidc" or not password:
        raise SystemExit(
            "OIDC integration requires sourced production settings and KEYCLOAK_TEST_USER_PASSWORD"
        )
    issuer = settings.oidc_issuer
    assert issuer and settings.oidc_audience and settings.oidc_public_key
    token_url = f"{issuer}/protocol/openid-connect/token"
    body = urllib.parse.urlencode(
        {
            "grant_type": "password",
            "client_id": "ragops-api",
            "username": "integration-user",
            "password": password,
        }
    ).encode()
    try:
        with urllib.request.urlopen(
            urllib.request.Request(token_url, data=body, method="POST"), timeout=10
        ) as response:  # noqa: S310
            token = str(json.loads(response.read())["access_token"])
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = {}
        error = payload.get("error") if isinstance(payload, dict) else None
        description = payload.get("error_description") if isinstance(payload, dict) else None
        details = ": ".join(str(value) for value in (error, description) if value)
        raise SystemExit(
            f"OIDC token request failed ({exc.code}): {details or 'unknown error'}"
        ) from exc
    payload = token.split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    roles = claims.get("resource_access", {}).get("ragops-api", {}).get("roles", [])
    if not isinstance(roles, list) or "viewer" not in roles:
        raise SystemExit("OIDC token does not contain viewer in resource_access.ragops-api.roles")
    context = OIDCIdentityProvider(
        issuer,
        settings.oidc_audience,
        settings.oidc_public_key,
        settings.oidc_algorithms,
        settings.oidc_tenant_claim,
        settings.oidc_roles_claim,
    ).authenticate(token)
    try:
        OIDCIdentityProvider(
            issuer,
            "wrong-audience",
            settings.oidc_public_key,
            settings.oidc_algorithms,
            settings.oidc_tenant_claim,
            settings.oidc_roles_claim,
        ).authenticate(token)
    except AuthenticationError:
        pass
    else:
        raise SystemExit("wrong audience was accepted")
    print(
        "OIDC PASS: roles_claim=resource_access.ragops-api.roles viewer_present=true "
        f"subject={context.user_id} tenant={context.tenant_id} roles={','.join(context.roles)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

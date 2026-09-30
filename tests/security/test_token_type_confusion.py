"""Only access tokens for approved clients are accepted; ID and refresh tokens are not."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
import pytest
from tests.security.oidc_helpers import AUDIENCE, ISSUER

from ragops.auth.identity import AuthenticationError, OIDCIdentityProvider


def token(private_key: str, *, headers: dict[str, str] | None = None, **extra: object) -> str:
    claims = {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "user-a",
        "tenant_id": "tenant-alpha",
        "roles": ["viewer"],
        "exp": datetime.now(UTC) + timedelta(minutes=5),
        **extra,
    }
    return jwt.encode(claims, private_key, algorithm="RS256", headers=headers)


def provider(public_key: str, allowed_clients: tuple[str, ...] = ()) -> OIDCIdentityProvider:
    return OIDCIdentityProvider(ISSUER, AUDIENCE, public_key, allowed_clients=allowed_clients)


@pytest.mark.parametrize("token_type", ["ID", "Refresh", "Logout", "id_token"])
def test_non_access_token_types_are_rejected(keys, token_type) -> None:
    with pytest.raises(AuthenticationError):
        provider(keys[1]).authenticate(token(keys[0], typ=token_type))


@pytest.mark.parametrize("header_type", ["JWT", "at+jwt", "application/at+jwt"])
def test_access_tokens_are_accepted(keys, header_type) -> None:
    context = provider(keys[1]).authenticate(
        token(keys[0], headers={"typ": header_type}, typ="Bearer")
    )
    assert context.tenant_id == "tenant-alpha"


def test_logout_header_type_is_rejected(keys) -> None:
    with pytest.raises(AuthenticationError):
        provider(keys[1]).authenticate(token(keys[0], headers={"typ": "logout+jwt"}))


def test_authorized_party_allowlist_is_enforced_when_configured(keys) -> None:
    approved = provider(keys[1], ("ragops-dashboard",))
    assert approved.authenticate(token(keys[0], azp="ragops-dashboard")).user_id == "user-a"
    for other in ({"azp": "other-client"}, {}):
        with pytest.raises(AuthenticationError):
            approved.authenticate(token(keys[0], **other))
    assert provider(keys[1]).authenticate(token(keys[0], azp="any")).user_id == "user-a"


def test_unknown_critical_header_extension_is_rejected(keys) -> None:
    # CVE-2026-32597: tokens that declare a critical extension the verifier does not
    # understand must be rejected (RFC 7515 section 4.1.11); fixed by PyJWT 2.13.0.
    crit = token(keys[0], headers={"crit": ["urn:rc:unknown"], "urn:rc:unknown": True})
    with pytest.raises(AuthenticationError):
        provider(keys[1]).authenticate(crit)

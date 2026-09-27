from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from ragops.api.app import resolve_authenticated_identity
from ragops.api.schemas import QueryApiRequest
from ragops.auth.dependencies import admin_dependency, current_identity, identity_provider_for
from ragops.auth.identity import (
    AuthenticatedUserContext,
    AuthenticationError,
    DeterministicDevelopmentIdentityProvider,
    OIDCIdentityProvider,
)
from ragops.config.settings import Settings


@pytest.fixture(scope="module")
def keys() -> tuple[str, str]:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    from cryptography.hazmat.primitives import serialization

    private_pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public_pem = (
        private.public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private_pem, public_pem


def provider(public_key: str) -> OIDCIdentityProvider:
    return OIDCIdentityProvider(
        issuer="https://issuer.synthetic.example/",
        audience="ragops-api",
        public_key=public_key,
    )


def token(private_key: str, **changes: object) -> str:
    claims: dict[str, object] = {
        "iss": "https://issuer.synthetic.example/",
        "aud": "ragops-api",
        "sub": "user-123",
        "tenant_id": "tenant-alpha",
        "roles": ["sales"],
        "name": "Synthetic User",
        "email": "user@example.invalid",
        "exp": datetime.now(UTC) + timedelta(minutes=5),
    }
    claims.update(changes)
    return jwt.encode(claims, private_key, algorithm="RS256")


def test_valid_jwt_maps_authoritative_context(keys: tuple[str, str]) -> None:
    context = provider(keys[1]).authenticate(token(keys[0]))
    assert context == AuthenticatedUserContext(
        user_id="user-123",
        tenant_id="tenant-alpha",
        roles=("sales",),
        display_name="Synthetic User",
        email="user@example.invalid",
        issuer="https://issuer.synthetic.example/",
        subject="user-123",
    )


def test_nested_provider_claim_mapping_supports_keycloak_style_roles(
    keys: tuple[str, str],
) -> None:
    configured = OIDCIdentityProvider(
        issuer="https://issuer.synthetic.example/",
        audience="ragops-api",
        public_key=keys[1],
        tenant_claim="realm.tenant",
        roles_claim="realm_access.roles",
    )
    claims_token = token(
        keys[0],
        tenant_id=None,
        roles=None,
        realm={"tenant": "tenant-alpha"},
        realm_access={"roles": ["admin"]},
    )
    assert configured.authenticate(claims_token).is_admin


@pytest.mark.parametrize(
    "changes",
    [
        {"exp": datetime.now(UTC) - timedelta(minutes=5)},
        {"iss": "https://attacker.invalid/"},
        {"aud": "other-api"},
        {"sub": None},
        {"tenant_id": None},
        {"roles": None},
        {"roles": ["superuser"]},
    ],
)
def test_invalid_claims_are_rejected(keys: tuple[str, str], changes: dict[str, object]) -> None:
    with pytest.raises(AuthenticationError):
        provider(keys[1]).authenticate(token(keys[0], **changes))


def test_invalid_signature_and_malformed_token_rejected(keys: tuple[str, str]) -> None:
    other_private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    from cryptography.hazmat.primitives import serialization

    other_pem = other_private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    with pytest.raises(AuthenticationError):
        provider(keys[1]).authenticate(token(other_pem))
    for malformed in ("", "abc", "a.b.c.d", "eyJhbGciOiJub25lIn0.eyJzdWIiOiJ4In0."):
        with pytest.raises(AuthenticationError):
            provider(keys[1]).authenticate(malformed)


def test_forged_admin_role_requires_valid_signature(keys: tuple[str, str]) -> None:
    forged = token(keys[0], roles=["admin"])
    assert provider(keys[1]).authenticate(forged).is_admin


def test_development_provider_is_environment_bounded() -> None:
    context = DeterministicDevelopmentIdentityProvider("demo").authenticate()
    assert context.tenant_id == "tenant-alpha"
    with pytest.raises(AuthenticationError):
        DeterministicDevelopmentIdentityProvider("production")
    with pytest.raises(AuthenticationError):
        DeterministicDevelopmentIdentityProvider("demo").authenticate("token")


def test_oidc_configuration_fails_closed() -> None:
    with pytest.raises(RuntimeError, match="issuer, audience and public key"):
        identity_provider_for(Settings(environment="production", identity_provider="oidc"))
    with pytest.raises(RuntimeError, match="Production requires"):
        identity_provider_for(Settings(environment="production", identity_provider="development"))


def test_dependency_returns_401_without_bearer(keys: tuple[str, str]) -> None:
    settings = Settings(
        environment="development",
        identity_provider="oidc",
        oidc_issuer="https://issuer.synthetic.example/",
        oidc_audience="ragops-api",
        oidc_public_key=keys[1],
    )
    with pytest.raises(HTTPException) as error:
        current_identity(settings, None)
    assert error.value.status_code == 401


def test_dependency_rejects_invalid_bearer(keys: tuple[str, str]) -> None:
    settings = Settings(
        environment="development",
        identity_provider="oidc",
        oidc_issuer="https://issuer.synthetic.example/",
        oidc_audience="ragops-api",
        oidc_public_key=keys[1],
    )
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="broken")
    with pytest.raises(HTTPException) as error:
        current_identity(settings, credentials)
    assert error.value.status_code == 401


def test_client_identity_override_is_ignored_for_oidc(keys: tuple[str, str]) -> None:
    trusted = provider(keys[1]).authenticate(token(keys[0]))
    request = QueryApiRequest(
        question="synthetic",
        tenant_id="tenant-beta",
        user_id="attacker",
        role="admin",
    )
    settings = Settings(
        environment="development",
        identity_provider="oidc",
        oidc_issuer="https://issuer.synthetic.example/",
        oidc_audience="ragops-api",
        oidc_public_key=keys[1],
    )
    resolved = resolve_authenticated_identity(settings, trusted, request)
    assert resolved == trusted


def test_admin_dependency_rejects_non_admin_claim(keys: tuple[str, str]) -> None:
    settings = Settings(
        environment="development",
        identity_provider="oidc",
        oidc_issuer="https://issuer.synthetic.example/",
        oidc_audience="ragops-api",
        oidc_public_key=keys[1],
    )
    trusted = provider(keys[1]).authenticate(token(keys[0]))
    with pytest.raises(HTTPException) as error:
        admin_dependency(settings)(trusted)
    assert error.value.status_code == 403

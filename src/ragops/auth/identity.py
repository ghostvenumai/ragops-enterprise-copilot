"""Verified identity boundary for the API.

The development provider is deliberately deterministic and is only valid for
local development/test/demo environments. OIDC uses a configured public key
and PyJWT's claim validation; it never accepts an unsigned token or a client
supplied identity claim.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import jwt

from ragops.storage.models import Role

DEVELOPMENT_ENVIRONMENTS = frozenset({"local", "development", "test", "demo"})
SUPPORTED_ALGORITHMS = frozenset({"RS256", "RS384", "RS512", "ES256", "ES384", "ES512"})


class AuthenticationError(ValueError):
    """Raised when credentials cannot establish a trusted identity."""


@dataclass(frozen=True)
class AuthenticatedUserContext:
    """Immutable identity derived from a verified token or explicit dev config."""

    user_id: str
    tenant_id: str
    roles: tuple[Role, ...]
    display_name: str | None = None
    email: str | None = None
    issuer: str | None = None
    subject: str | None = None

    def __post_init__(self) -> None:
        if not self.user_id or not self.tenant_id or not self.roles:
            raise ValueError("Authenticated context requires user, tenant and role")

    @property
    def is_admin(self) -> bool:
        return "admin" in self.roles

    @property
    def role(self) -> Role:
        return "admin" if self.is_admin else self.roles[0]


class IdentityProvider(Protocol):
    def authenticate(self, token: str | None = None) -> AuthenticatedUserContext:
        """Return an authenticated context or raise AuthenticationError."""


def _roles(value: Any) -> tuple[Role, ...]:
    values = [value] if isinstance(value, str) else value
    if not isinstance(values, list | tuple) or not values:
        raise AuthenticationError("role claim is missing or malformed")
    allowed = {"viewer", "sales", "support", "operations", "compliance", "admin"}
    result: list[Role] = []
    for role in values:
        if not isinstance(role, str) or role not in allowed:
            raise AuthenticationError("role claim contains an unsupported role")
        if role not in result:
            result.append(role)  # type: ignore[arg-type]
    return tuple(result)


@dataclass(frozen=True)
class DeterministicDevelopmentIdentityProvider:
    """Explicit local identity; never construct this provider in production."""

    environment: str
    user_id: str = "demo-user"
    tenant_id: str = "tenant-alpha"
    roles: tuple[Role, ...] = ("sales",)
    display_name: str = "Development User"
    email: str | None = None

    def __post_init__(self) -> None:
        if self.environment not in DEVELOPMENT_ENVIRONMENTS:
            raise AuthenticationError("development identity is unavailable in this environment")
        if not self.user_id or not self.tenant_id:
            raise AuthenticationError("development identity requires user and tenant")
        _roles(self.roles)

    def authenticate(self, token: str | None = None) -> AuthenticatedUserContext:
        if token is not None:
            raise AuthenticationError("development identity does not accept bearer tokens")
        return AuthenticatedUserContext(
            user_id=self.user_id,
            tenant_id=self.tenant_id,
            roles=self.roles,
            display_name=self.display_name,
            email=self.email,
            issuer="development",
            subject=self.user_id,
        )


@dataclass(frozen=True)
class OIDCIdentityProvider:
    """Validate a JWT signed by a configured OIDC issuer.

    A public key is injected through configuration (or a secret-mounted file).
    JWKS retrieval is intentionally outside request handling; deployments may
    rotate keys by refreshing this configuration through their OIDC control
    plane. Provider-specific claim names are configurable for Entra, Keycloak,
    Auth0 and generic OIDC without hard-coded provider behavior.
    """

    issuer: str
    audience: str
    public_key: str
    algorithms: tuple[str, ...] = ("RS256",)
    tenant_claim: str = "tenant_id"
    roles_claim: str = "roles"
    # When set, the authorized party (azp, or client_id) must be one of these clients.
    allowed_clients: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.issuer or not self.audience or not self.public_key:
            raise AuthenticationError("OIDC issuer, audience and public key are required")
        if not self.algorithms or any(
            algorithm not in SUPPORTED_ALGORITHMS for algorithm in self.algorithms
        ):
            raise AuthenticationError("OIDC algorithms must be approved asymmetric algorithms")
        if not self.tenant_claim or not self.roles_claim:
            raise AuthenticationError("OIDC claim mappings are required")

    def authenticate(self, token: str | None = None) -> AuthenticatedUserContext:
        if not token or token.count(".") != 2:
            raise AuthenticationError("malformed bearer token")
        try:
            claims = jwt.decode(
                token,
                self.public_key,
                algorithms=list(self.algorithms),
                issuer=self.issuer,
                audience=self.audience,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except (jwt.InvalidTokenError, TypeError, ValueError) as exc:
            raise AuthenticationError("token validation failed") from exc
        self._require_access_token(token, claims)
        subject = claims.get("sub")
        tenant = _claim_value(claims, self.tenant_claim)
        if not isinstance(subject, str) or not subject:
            raise AuthenticationError("subject claim is missing")
        if not isinstance(tenant, str) or not tenant:
            raise AuthenticationError("tenant claim is missing")
        return AuthenticatedUserContext(
            user_id=subject,
            tenant_id=tenant,
            roles=_roles(_claim_value(claims, self.roles_claim)),
            display_name=_optional_string(claims.get("name")),
            email=_optional_string(claims.get("email"))
            or _optional_string(claims.get("preferred_username")),
            issuer=self.issuer,
            subject=subject,
        )

    def _require_access_token(self, token: str, claims: dict[str, Any]) -> None:
        """Reject ID, refresh and logout tokens and unapproved clients (token confusion)."""
        header_type = jwt.get_unverified_header(token).get("typ")
        if header_type is not None and str(header_type).lower() not in _ACCESS_HEADER_TYPES:
            raise AuthenticationError("token type is not an access token")
        claim_type = claims.get("typ")
        if claim_type is not None and str(claim_type).lower() not in _ACCESS_CLAIM_TYPES:
            raise AuthenticationError("token type is not an access token")
        if self.allowed_clients:
            party = claims.get("azp") or claims.get("client_id")
            if party not in self.allowed_clients:
                raise AuthenticationError("token was issued to an unapproved client")


_ACCESS_HEADER_TYPES = frozenset({"jwt", "at+jwt", "application/at+jwt"})
_ACCESS_CLAIM_TYPES = frozenset({"bearer", "at+jwt"})


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _claim_value(claims: dict[str, Any], claim_name: str) -> Any:
    """Read a direct or dotted nested claim (for example realm_access.roles)."""

    value: Any = claims
    for component in claim_name.split("."):
        if not isinstance(value, dict) or component not in value:
            return None
        value = value[component]
    return value

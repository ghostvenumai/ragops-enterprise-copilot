"""FastAPI dependencies enforcing authentication before application logic."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, HTTPException, Request, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ragops.auth.identity import (
    AuthenticatedUserContext,
    AuthenticationError,
    DeterministicDevelopmentIdentityProvider,
    IdentityProvider,
    OIDCIdentityProvider,
)
from ragops.config.settings import Settings

bearer = HTTPBearer(auto_error=False)


def identity_provider_for(settings: Settings) -> IdentityProvider:
    settings.validate_identity_configuration()
    if settings.identity_provider == "development":
        return DeterministicDevelopmentIdentityProvider(
            environment=settings.environment,
            user_id=settings.development_user_id,
            tenant_id=settings.development_tenant_id,
            roles=tuple(settings.development_roles),  # type: ignore[arg-type]
        )
    assert settings.oidc_issuer and settings.oidc_audience and settings.oidc_public_key
    return OIDCIdentityProvider(
        issuer=settings.oidc_issuer,
        audience=settings.oidc_audience,
        public_key=settings.oidc_public_key,
        algorithms=settings.oidc_algorithms,
        tenant_claim=settings.oidc_tenant_claim,
        roles_claim=settings.oidc_roles_claim,
    )


def current_identity(
    settings: Settings,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)],
) -> AuthenticatedUserContext:
    provider = identity_provider_for(settings)
    token = credentials.credentials if credentials else None
    try:
        return provider.authenticate(token)
    except (AuthenticationError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication failed",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


def identity_dependency(settings: Settings) -> Callable[..., AuthenticatedUserContext]:
    def dependency(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Security(bearer)],
    ) -> AuthenticatedUserContext:
        return current_identity(settings, credentials)

    return dependency


def admin_dependency(
    settings: Settings,
) -> Callable[[AuthenticatedUserContext], AuthenticatedUserContext]:
    def dependency(
        identity: Annotated[AuthenticatedUserContext, Depends(identity_dependency(settings))],
    ) -> AuthenticatedUserContext:
        if not identity.is_admin:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin role required")
        return identity

    return dependency


def request_identity(request: Request) -> AuthenticatedUserContext:
    identity = getattr(request.state, "identity", None)
    if not isinstance(identity, AuthenticatedUserContext):
        raise HTTPException(status_code=401, detail="Authentication required")
    return identity

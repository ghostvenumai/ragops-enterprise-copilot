"""Role-based access control for tenants and document access levels."""

from __future__ import annotations

from ragops.storage.models import AccessLevel, QueryUser, Role

ACCESS_RANK: dict[AccessLevel, int] = {
    "public": 0,
    "internal": 1,
    "confidential": 2,
    "restricted": 3,
}

ROLE_MAX_ACCESS: dict[Role, AccessLevel] = {
    "viewer": "internal",
    "sales": "confidential",
    "support": "confidential",
    "operations": "confidential",
    "compliance": "restricted",
    "admin": "restricted",
}


class AuthorizationError(Exception):
    """Raised when a user is not allowed to access a resource."""


def can_access_level(role: Role, access_level: AccessLevel) -> bool:
    return ACCESS_RANK[access_level] <= ACCESS_RANK[ROLE_MAX_ACCESS[role]]


def require_tenant(user: QueryUser, tenant_id: str) -> None:
    if user.role != "admin" and user.tenant_id != tenant_id:
        raise AuthorizationError("cross-tenant access denied")


def require_document_access(user: QueryUser, tenant_id: str, access_level: AccessLevel) -> None:
    require_tenant(user, tenant_id)
    if not can_access_level(user.role, access_level):
        raise AuthorizationError("role lacks document access")

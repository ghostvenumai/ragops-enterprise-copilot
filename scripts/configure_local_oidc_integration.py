"""Bootstrap the local Keycloak realm and write only non-secret OIDC config.

The product client ``ragops-api`` never accepts passwords directly. The password grant
the local integration check needs lives on a separate, local-only client that the gate
removes again (``--cleanup``). Before any change the realm configuration is snapshotted
(redacted, owner-only); afterwards the two client settings are verified and everything
the bootstrap does not manage must be unchanged.
"""
# ruff: noqa: E501, S310

from __future__ import annotations

import base64
import hashlib
import json
import os
import shlex
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

ROOT = Path(__file__).resolve().parents[1]
REALM = "ragops-integration"
BASE = "http://localhost:8081"
API_CLIENT = "ragops-api"
INTEGRATION_CHECK_CLIENT = "ragops-integration-check"
INTEGRATION_USER = "integration-user"
INTEGRATION_TENANT_ID = "integration-tenant"
RAGOPS_ROLES = ("viewer", "sales", "support", "operations", "compliance", "admin")
ISSUER = f"http://localhost:8081/realms/{REALM}"
# Bounded login attempts: temporary lockout after repeated failures, never permanent, and
# the same generic error for unknown users and wrong passwords.
REALM_HARDENING: dict[str, object] = {
    "bruteForceProtected": True,
    "permanentLockout": False,
    "failureFactor": 10,
    "waitIncrementSeconds": 60,
    "maxFailureWaitSeconds": 900,
    "maxDeltaTimeSeconds": 43200,
    "registrationAllowed": False,
    "sslRequired": "external",
}
# The product client: no resource-owner password grant.
API_CLIENT_CONFIG: dict[str, object] = {
    "clientId": API_CLIENT,
    "enabled": True,
    "publicClient": True,
    "directAccessGrantsEnabled": False,
    "protocol": "openid-connect",
    "attributes": {"access.token.claim": "true"},
}
# Local-only and gate-only: the single client that may exchange the synthetic
# integration user's password for a token. It has no browser flows and is removed by
# ``--cleanup``.
INTEGRATION_CHECK_CLIENT_CONFIG: dict[str, object] = {
    "clientId": INTEGRATION_CHECK_CLIENT,
    "name": "Local OIDC integration check (gate-only)",
    "enabled": True,
    "publicClient": True,
    "directAccessGrantsEnabled": True,
    "standardFlowEnabled": False,
    "implicitFlowEnabled": False,
    "serviceAccountsEnabled": False,
    "protocol": "openid-connect",
}
EXPECTED_DIRECT_ACCESS_GRANTS = {API_CLIENT: False, INTEGRATION_CHECK_CLIENT: True}
MANAGED_CLIENTS = frozenset(EXPECTED_DIRECT_ACCESS_GRANTS)
MANAGED_REALM_KEYS = frozenset({"enabled", *REALM_HARDENING})
ENV_PATH = Path(os.getenv("RAGOPS_INTEGRATION_ENV", str(Path.home() / "ragops-integration.env")))
SNAPSHOT_PATH = Path(
    os.getenv("RAGOPS_OIDC_SNAPSHOT", str(ROOT / ".loop" / "tmp" / "oidc-realm-snapshot.json"))
)
REDACTED = "[REDACTED]"
_SENSITIVE_KEY_PARTS = ("secret", "password", "credential", "token", "privatekey")


def request(
    url: str, method: str = "GET", data: object | None = None, token: str | None = None
) -> tuple[int, dict[str, object]]:
    body = json.dumps(data).encode() if data is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, data=body, headers=headers, method=method), timeout=5
        ) as response:  # noqa: S310
            raw = response.read()
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        return exc.code, json.loads(raw) if raw else {}


def redact(value: Any) -> Any:
    """Copy a Keycloak representation without any secret-bearing field."""
    if isinstance(value, dict):
        return {
            str(key): (
                REDACTED
                if any(part in str(key).lower().replace(".", "") for part in _SENSITIVE_KEY_PARTS)
                else redact(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def realm_state(base: str, token: str) -> dict[str, Any]:
    """Redacted realm configuration: settings, clients and user names."""
    status, realm = request(f"{base}/admin/realms/{REALM}", token=token)
    if status == 404:
        return {"realm_exists": False, "realm": {}, "clients": [], "users": []}
    if status != 200:
        raise SystemExit("Keycloak realm configuration could not be read")
    clients: Any = request(f"{base}/admin/realms/{REALM}/clients?max=1000", token=token)[1]
    users: Any = request(
        f"{base}/admin/realms/{REALM}/users?max=1000&briefRepresentation=true", token=token
    )[1]
    return {
        "realm_exists": True,
        "realm": redact(realm),
        "clients": redact(sorted(clients, key=lambda item: str(item.get("clientId")))),
        "users": sorted(str(user.get("username")) for user in users),
    }


def unrelated_state(state: dict[str, Any]) -> dict[str, Any]:
    """Everything the bootstrap does not manage; must be identical before and after."""
    return {
        "realm": {
            key: value for key, value in state["realm"].items() if key not in MANAGED_REALM_KEYS
        },
        "clients": [
            client for client in state["clients"] if client.get("clientId") not in MANAGED_CLIENTS
        ],
        "users": [name for name in state["users"] if name != INTEGRATION_USER],
    }


def state_digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def write_snapshot(state: dict[str, Any], path: Path = SNAPSHOT_PATH) -> str:
    """Persist the redacted pre-bootstrap configuration owner-only; returns its digest."""
    digest = state_digest(state)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "realm": REALM,
        "taken_at": datetime.now(UTC).isoformat(),
        "sha256": digest,
        "state": state,
    }
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
    path.chmod(0o600)
    return digest


def client_setting_errors(state: dict[str, Any]) -> list[str]:
    """Deviations from the required password-grant settings of the two managed clients."""
    by_id = {str(client.get("clientId")): client for client in state["clients"]}
    errors = []
    for client_id, expected in EXPECTED_DIRECT_ACCESS_GRANTS.items():
        client = by_id.get(client_id)
        if client is None:
            errors.append(f"{client_id} is missing")
        elif client.get("directAccessGrantsEnabled") is not expected:
            errors.append(f"{client_id} directAccessGrantsEnabled is not {str(expected).lower()}")
    return errors


def admin_token(base: str, credentials: tuple[str, str] | None = None) -> str:
    # Must match docker-compose.integration.yml's Keycloak 25 default.
    admin = os.getenv("KEYCLOAK_ADMIN", "admin")
    password = os.getenv("KEYCLOAK_ADMIN_PASSWORD")
    if credentials:
        admin, password = credentials
    if not password:
        raise SystemExit("KEYCLOAK_ADMIN_PASSWORD must be supplied outside Git")
    for _ in range(60):
        try:
            request(f"{base}/realms/master/.well-known/openid-configuration")
            break
        except (urllib.error.URLError, TimeoutError):
            time.sleep(2)
    else:
        raise SystemExit("Keycloak did not become ready")
    token_data = urllib.parse.urlencode(
        {
            "username": admin,
            "password": password,
            "grant_type": "password",
            "client_id": "admin-cli",
        }
    ).encode()
    try:
        with urllib.request.urlopen(
            urllib.request.Request(
                f"{base}/realms/master/protocol/openid-connect/token",
                data=token_data,
                method="POST",
            ),
            timeout=10,
        ) as response:  # noqa: S310
            return str(json.loads(response.read())["access_token"])
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"Keycloak admin login failed ({exc.code})") from None


def ensure_client(base: str, token: str, config: dict[str, object]) -> str:
    """Create or reconcile one client and return its internal id."""
    query = f"{base}/admin/realms/{REALM}/clients?clientId={config['clientId']}"
    status, _ = request(query, token=token)
    client_records: Any = request(query, token=token)[1]
    if status == 200 and not client_records:
        request(f"{base}/admin/realms/{REALM}/clients", "POST", config, token)
        client_records = request(query, token=token)[1]
    if not client_records:
        raise SystemExit(f"Keycloak client {config['clientId']} could not be created")
    internal_id = str(client_records[0]["id"])
    # Reconcile existing clients as well; Keycloak persists old flags in its volume.
    request(f"{base}/admin/realms/{REALM}/clients/{internal_id}", "PUT", config, token)
    return internal_id


def reconcile_mappers(base: str, token: str, internal_id: str) -> None:
    """Audience and tenant claims, identical for every client that issues API tokens."""
    mapper_url = f"{base}/admin/realms/{REALM}/clients/{internal_id}/protocol-mappers/models"
    audience_mapper = {
        "name": "ragops-api-audience",
        "protocol": "openid-connect",
        "protocolMapper": "oidc-audience-mapper",
        "consentRequired": False,
        "config": {
            "included.custom.audience": "ragops-api",
            "access.token.claim": "true",
            "id.token.claim": "false",
            "introspection.token.claim": "true",
        },
    }
    existing_mappers: Any = request(mapper_url, token=token)[1]
    existing = next(
        (item for item in existing_mappers if item.get("name") == audience_mapper["name"]), None
    )
    if existing:
        request(f"{mapper_url}/{existing['id']}", "PUT", audience_mapper, token)
    else:
        request(mapper_url, "POST", audience_mapper, token)
    tenant_mapper = {
        "name": "tenant-id",
        "protocol": "openid-connect",
        "protocolMapper": "oidc-usermodel-attribute-mapper",
        "consentRequired": False,
        "config": {
            "user.attribute": "tenant_id",
            "claim.name": "tenant_id",
            "jsonType.label": "String",
            "access.token.claim": "true",
            "id.token.claim": "false",
            "userinfo.token.claim": "true",
            "introspection.token.claim": "true",
            "multivalued": "false",
        },
    }
    existing_mappers = request(mapper_url, token=token)[1]
    for obsolete in (item for item in existing_mappers if item.get("name") == "tenant_id"):
        request(f"{mapper_url}/{obsolete['id']}", "DELETE", token=token)
    existing = next((item for item in existing_mappers if item.get("name") == "tenant-id"), None)
    if existing:
        request(f"{mapper_url}/{existing['id']}", "PUT", tenant_mapper, token)
    else:
        request(mapper_url, "POST", tenant_mapper, token)


def reconcile_profile_and_user(base: str, token: str, internal_id: str) -> None:
    client_roles_url = f"{base}/admin/realms/{REALM}/clients/{internal_id}/roles"
    profile_url = f"{base}/admin/realms/{REALM}/users/profile"
    profile_status, profile = request(profile_url, token=token)
    if profile_status != 200:
        raise SystemExit("Keycloak user profile could not be read")
    profile_attributes = profile.get("attributes", [])
    if not isinstance(profile_attributes, list):
        raise SystemExit("Keycloak user profile attributes are malformed")
    managed = {
        "name": "tenant_id",
        "displayName": "Tenant ID",
        "multivalued": False,
        "permissions": {"view": ["admin"], "edit": ["admin"]},
    }
    profile_attributes = [item for item in profile_attributes if item.get("name") != "tenant_id"]
    profile_attributes.append(managed)
    request(profile_url, "PUT", {**profile, "attributes": profile_attributes}, token)
    test_password = os.getenv("KEYCLOAK_TEST_USER_PASSWORD")
    if not test_password:
        return
    users: Any = request(
        f"{base}/admin/realms/{REALM}/users?username=integration-user", token=token
    )[1]
    user_payload = {
        "username": "integration-user",
        "firstName": "Integration",
        "lastName": "User",
        "enabled": True,
        "email": "integration-user@ragops.local",
        "emailVerified": True,
        "requiredActions": [],
        "attributes": {"tenant_id": [INTEGRATION_TENANT_ID]},
        "credentials": [{"type": "password", "value": test_password, "temporary": False}],
    }
    user_id = str(users[0]["id"]) if users else ""
    if users:
        request(f"{base}/admin/realms/{REALM}/users/{user_id}", "PUT", user_payload, token)
    else:
        request(f"{base}/admin/realms/{REALM}/users", "POST", user_payload, token)
        users = request(
            f"{base}/admin/realms/{REALM}/users?username=integration-user", token=token
        )[1]
        user_id = str(users[0]["id"]) if users else ""
    if not user_id:
        return
    verified_user: Any = request(f"{base}/admin/realms/{REALM}/users/{user_id}", token=token)[1]
    if verified_user.get("attributes", {}).get("tenant_id") != [INTEGRATION_TENANT_ID]:
        raise SystemExit("Keycloak did not persist integration-user tenant_id")
    role = request(f"{client_roles_url}/viewer", token=token)[1]
    realm_role_status, realm_role = request(
        f"{base}/admin/realms/{REALM}/roles/viewer", token=token
    )
    if realm_role_status == 200:
        request(
            f"{base}/admin/realms/{REALM}/users/{user_id}/role-mappings/realm",
            "DELETE",
            [realm_role],
            token,
        )
    request(
        f"{base}/admin/realms/{REALM}/users/{user_id}/role-mappings/clients/{internal_id}",
        "POST",
        [role],
        token,
    )


def write_env() -> str:
    discovery = request(f"{ISSUER}/.well-known/openid-configuration")[1]
    jwks: Any = request(str(discovery["jwks_uri"]))[1]
    key: dict[str, Any] = next(
        item
        for item in jwks["keys"]
        if item.get("kty") == "RSA" and item.get("use", "sig") == "sig"
    )

    def b64(value: object) -> bytes:
        encoded = str(value)
        return base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))

    pem = (
        RSAPublicNumbers(int.from_bytes(b64(key["e"]), "big"), int.from_bytes(b64(key["n"]), "big"))
        .public_key()
        .public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    values = {
        line.split("=", 1)[0]: line.split("=", 1)[1]
        for line in lines
        if "=" in line and not line.startswith("#")
    }
    values.update(
        {
            "RAGOPS_OIDC_ISSUER": ISSUER,
            "RAGOPS_OIDC_AUDIENCE": "ragops-api",
            "RAGOPS_OIDC_ROLES_CLAIM": "resource_access.ragops-api.roles",
        }
    )
    serialized = "\n".join(f"{name}={shlex.quote(value)}" for name, value in values.items())
    serialized += f"\nRAGOPS_OIDC_PUBLIC_KEY={shlex.quote(pem)}\n"
    ENV_PATH.write_text(serialized, encoding="utf-8")
    ENV_PATH.chmod(0o600)
    return str(key.get("kid", "unknown"))


def bootstrap(base: str = BASE) -> int:
    token = admin_token(base)
    before = realm_state(base, token)
    snapshot_digest = write_snapshot(before)
    realm = {
        "realm": REALM,
        "enabled": True,
        "roles": {"realm": [{"name": "viewer"}, {"name": "admin"}]},
        **REALM_HARDENING,
    }
    if before["realm_exists"]:
        request(f"{base}/admin/realms/{REALM}", "PUT", realm, token)
    else:
        request(f"{base}/admin/realms", "POST", realm, token)
    api_internal_id = ensure_client(base, token, API_CLIENT_CONFIG)
    client_roles_url = f"{base}/admin/realms/{REALM}/clients/{api_internal_id}/roles"
    for role_name in RAGOPS_ROLES:
        role_status, _ = request(f"{client_roles_url}/{role_name}", token=token)
        if role_status == 404:
            request(client_roles_url, "POST", {"name": role_name}, token)
    reconcile_mappers(base, token, api_internal_id)
    check_internal_id = ensure_client(base, token, INTEGRATION_CHECK_CLIENT_CONFIG)
    reconcile_mappers(base, token, check_internal_id)
    reconcile_profile_and_user(base, token, api_internal_id)
    after = realm_state(base, token)
    errors = client_setting_errors(after)
    if errors:
        raise SystemExit("Keycloak client verification failed: " + "; ".join(errors))
    if before["realm_exists"] and unrelated_state(before) != unrelated_state(after):
        raise SystemExit("Keycloak bootstrap changed unrelated realm state")
    kid = write_env()
    print(
        f"OIDC integration configured: issuer={ISSUER} audience=ragops-api kid={kid} "
        f"ragops-api.directAccessGrantsEnabled=false "
        f"{INTEGRATION_CHECK_CLIENT}.directAccessGrantsEnabled=true "
        f"unrelated_state=unchanged snapshot_sha256={snapshot_digest[:12]}"
    )
    return 0


def cleanup(base: str = BASE) -> int:
    """Remove the gate-only client and snapshot; the product client stays hardened."""
    token = admin_token(base)
    before = realm_state(base, token)
    if not before["realm_exists"]:
        raise SystemExit("Keycloak realm does not exist")
    query = f"{base}/admin/realms/{REALM}/clients?clientId={INTEGRATION_CHECK_CLIENT}"
    records: Any = request(query, token=token)[1]
    for record in records:
        request(f"{base}/admin/realms/{REALM}/clients/{record['id']}", "DELETE", token=token)
    after = realm_state(base, token)
    by_id = {str(client.get("clientId")): client for client in after["clients"]}
    if INTEGRATION_CHECK_CLIENT in by_id:
        raise SystemExit(f"Keycloak cleanup left {INTEGRATION_CHECK_CLIENT} behind")
    if by_id.get(API_CLIENT, {}).get("directAccessGrantsEnabled") is not False:
        raise SystemExit("ragops-api directAccessGrantsEnabled is not false after cleanup")
    if unrelated_state(before) != unrelated_state(after):
        raise SystemExit("Keycloak cleanup changed unrelated realm state")
    SNAPSHOT_PATH.unlink(missing_ok=True)
    print(
        f"OIDC integration cleanup: {INTEGRATION_CHECK_CLIENT}=removed "
        "ragops-api.directAccessGrantsEnabled=false unrelated_state=unchanged snapshot=removed"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments not in ([], ["--cleanup"]):
        raise SystemExit("usage: configure_local_oidc_integration.py [--cleanup]")
    return cleanup() if arguments else bootstrap()


if __name__ == "__main__":
    raise SystemExit(main())

"""Bootstrap the local Keycloak realm and write only non-secret OIDC config."""
# ruff: noqa: E501, S310

from __future__ import annotations

import base64
import json
import os
import shlex
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

REALM = "ragops-integration"
INTEGRATION_TENANT_ID = "integration-tenant"
RAGOPS_ROLES = ("viewer", "sales", "support", "operations", "compliance", "admin")
ISSUER = f"http://localhost:8081/realms/{REALM}"
ENV_PATH = Path(os.getenv("RAGOPS_INTEGRATION_ENV", str(Path.home() / "ragops-integration.env")))


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


def main() -> int:
    # Must match docker-compose.integration.yml's Keycloak 25 default.
    admin = os.getenv("KEYCLOAK_ADMIN", "admin")
    password = os.getenv("KEYCLOAK_ADMIN_PASSWORD")
    if not password:
        raise SystemExit("KEYCLOAK_ADMIN_PASSWORD must be supplied outside Git")
    base = "http://localhost:8081"
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
    with urllib.request.urlopen(
        urllib.request.Request(
            f"{base}/realms/master/protocol/openid-connect/token", data=token_data, method="POST"
        ),
        timeout=10,
    ) as response:  # noqa: S310
        admin_token = str(json.loads(response.read())["access_token"])
    realm = {
        "realm": REALM,
        "enabled": True,
        "roles": {"realm": [{"name": "viewer"}, {"name": "admin"}]},
    }
    status, _ = request(f"{base}/admin/realms/{REALM}", "GET", token=admin_token)
    request(f"{base}/admin/realms", "POST", realm, admin_token) if status == 404 else request(
        f"{base}/admin/realms/{REALM}", "PUT", realm, admin_token
    )
    client = {
        "clientId": "ragops-api",
        "enabled": True,
        "publicClient": True,
        "directAccessGrantsEnabled": True,
        "protocol": "openid-connect",
        "attributes": {"access.token.claim": "true"},
    }
    status, _ = request(
        f"{base}/admin/realms/{REALM}/clients?clientId=ragops-api", token=admin_token
    )
    client_records: Any = request(
        f"{base}/admin/realms/{REALM}/clients?clientId=ragops-api", token=admin_token
    )[1]
    if status == 200 and not client_records:
        request(f"{base}/admin/realms/{REALM}/clients", "POST", client, admin_token)
        client_records = request(
            f"{base}/admin/realms/{REALM}/clients?clientId=ragops-api", token=admin_token
        )[1]
    if client_records:
        internal_id = str(client_records[0]["id"])
        # Reconcile existing clients as well; Keycloak persists old flags in its volume.
        request(f"{base}/admin/realms/{REALM}/clients/{internal_id}", "PUT", client, admin_token)
        client_roles_url = f"{base}/admin/realms/{REALM}/clients/{internal_id}/roles"
        for role_name in RAGOPS_ROLES:
            role_status, _ = request(f"{client_roles_url}/{role_name}", token=admin_token)
            if role_status == 404:
                request(client_roles_url, "POST", {"name": role_name}, admin_token)
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
        existing_mappers: Any = request(mapper_url, token=admin_token)[1]
        existing = next(
            (item for item in existing_mappers if item.get("name") == audience_mapper["name"]), None
        )
        if existing:
            request(f"{mapper_url}/{existing['id']}", "PUT", audience_mapper, admin_token)
        else:
            request(mapper_url, "POST", audience_mapper, admin_token)
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
        existing_mappers = request(mapper_url, token=admin_token)[1]
        for obsolete in (item for item in existing_mappers if item.get("name") == "tenant_id"):
            request(f"{mapper_url}/{obsolete['id']}", "DELETE", token=admin_token)
        existing = next(
            (item for item in existing_mappers if item.get("name") == "tenant-id"), None
        )
        if existing:
            request(f"{mapper_url}/{existing['id']}", "PUT", tenant_mapper, admin_token)
        else:
            request(mapper_url, "POST", tenant_mapper, admin_token)
        profile_url = f"{base}/admin/realms/{REALM}/users/profile"
        profile_status, profile = request(profile_url, token=admin_token)
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
        profile_attributes = [
            item for item in profile_attributes if item.get("name") != "tenant_id"
        ]
        profile_attributes.append(managed)
        request(profile_url, "PUT", {**profile, "attributes": profile_attributes}, admin_token)
        test_password = os.getenv("KEYCLOAK_TEST_USER_PASSWORD")
        if test_password:
            users: Any = request(
                f"{base}/admin/realms/{REALM}/users?username=integration-user", token=admin_token
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
                request(
                    f"{base}/admin/realms/{REALM}/users/{user_id}",
                    "PUT",
                    user_payload,
                    admin_token,
                )
            else:
                request(f"{base}/admin/realms/{REALM}/users", "POST", user_payload, admin_token)
                users = request(
                    f"{base}/admin/realms/{REALM}/users?username=integration-user",
                    token=admin_token,
                )[1]
                user_id = str(users[0]["id"]) if users else ""
            if user_id:
                verified_user: Any = request(
                    f"{base}/admin/realms/{REALM}/users/{user_id}", token=admin_token
                )[1]
                if verified_user.get("attributes", {}).get("tenant_id") != [INTEGRATION_TENANT_ID]:
                    raise SystemExit("Keycloak did not persist integration-user tenant_id")
                role = request(f"{client_roles_url}/viewer", token=admin_token)[1]
                realm_role_status, realm_role = request(
                    f"{base}/admin/realms/{REALM}/roles/viewer", token=admin_token
                )
                if realm_role_status == 200:
                    request(
                        f"{base}/admin/realms/{REALM}/users/{user_id}/role-mappings/realm",
                        "DELETE",
                        [realm_role],
                        admin_token,
                    )
                request(
                    f"{base}/admin/realms/{REALM}/users/{user_id}/role-mappings/clients/{internal_id}",
                    "POST",
                    [role],
                    admin_token,
                )
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
    print(
        f"OIDC integration configured: issuer={ISSUER} audience=ragops-api kid={key.get('kid', 'unknown')}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

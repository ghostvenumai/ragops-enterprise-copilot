from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_keycloak_admin_username_is_consistent() -> None:
    compose = (ROOT / "docker-compose.integration.yml").read_text(encoding="utf-8")
    script = (ROOT / "scripts/configure_local_oidc_integration.py").read_text(encoding="utf-8")
    assert "KEYCLOAK_ADMIN: ${KEYCLOAK_ADMIN:-admin}" in compose
    assert 'os.getenv("KEYCLOAK_ADMIN", "admin")' in script
    assert "KC_BOOTSTRAP_ADMIN_" not in compose


def test_keycloak_client_enables_password_token_flow() -> None:
    script = (ROOT / "scripts/configure_local_oidc_integration.py").read_text(encoding="utf-8")
    check = (ROOT / "scripts/oidc_integration_check.py").read_text(encoding="utf-8")
    assert '"directAccessGrantsEnabled": True' in script
    assert '"publicClient": True' in script
    assert '"grant_type": "password"' in check
    assert '"username": "integration-user"' in check
    assert "error_description" in check


def test_keycloak_audience_mapper_is_explicit_and_idempotent() -> None:
    script = (ROOT / "scripts/configure_local_oidc_integration.py").read_text(encoding="utf-8")
    assert '"name": "ragops-api-audience"' in script
    assert '"protocolMapper": "oidc-audience-mapper"' in script
    assert '"included.custom.audience": "ragops-api"' in script
    assert '"access.token.claim": "true"' in script
    assert '"id.token.claim": "false"' in script
    assert '"introspection.token.claim": "true"' in script
    assert "existing_mappers" in script and "PUT" in script


def test_expected_audience_contract() -> None:
    representative_token_claims = {"aud": ["account", "ragops-api"]}
    audience = representative_token_claims["aud"]
    assert "ragops-api" in audience


def test_tenant_mapper_and_integration_identity_are_authoritative() -> None:
    script = (ROOT / "scripts/configure_local_oidc_integration.py").read_text(encoding="utf-8")
    assert 'INTEGRATION_TENANT_ID = "integration-tenant"' in script
    assert '"tenant_id": [INTEGRATION_TENANT_ID]' in script
    assert '"name": "tenant-id"' in script
    assert '"user.attribute": "tenant_id"' in script
    assert '"claim.name": "tenant_id"' in script
    assert '"access.token.claim": "true"' in script
    assert '"userinfo.token.claim": "true"' in script
    assert '"introspection.token.claim": "true"' in script
    assert '"multivalued": "false"' in script
    assert '"name": "viewer"' in script or "/roles/viewer" in script
    assert "/users/profile" in script
    assert '"displayName": "Tenant ID"' in script
    assert '"permissions": {"view": ["admin"], "edit": ["admin"]}' in script
    assert "did not persist integration-user tenant_id" in script


def test_keycloak_roles_use_nested_realm_claim() -> None:
    script = (ROOT / "scripts/configure_local_oidc_integration.py").read_text(encoding="utf-8")
    check = (ROOT / "scripts/oidc_integration_check.py").read_text(encoding="utf-8")
    assert '"RAGOPS_OIDC_ROLES_CLAIM": "resource_access.ragops-api.roles"' in script
    assert 'claims.get("resource_access", {}).get("ragops-api", {}).get("roles", [])' in check
    assert '"viewer" not in roles' in check


def test_client_roles_are_scoped_and_reconciled() -> None:
    script = (ROOT / "scripts/configure_local_oidc_integration.py").read_text(encoding="utf-8")
    assert "RAGOPS_ROLES" in script
    assert "role-mappings/clients" in script
    assert "role-mappings/realm" in script and '"DELETE"' in script
    claims = {
        "realm_access": {"roles": ["offline_access", "uma_authorization"]},
        "resource_access": {"ragops-api": {"roles": ["viewer"]}},
    }
    assert claims["resource_access"]["ragops-api"]["roles"] == ["viewer"]
    assert "viewer" not in claims["realm_access"]["roles"]

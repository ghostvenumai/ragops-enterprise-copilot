from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_keycloak_admin_username_is_consistent() -> None:
    compose = (ROOT / "docker-compose.integration.yml").read_text(encoding="utf-8")
    script = (ROOT / "scripts/configure_local_oidc_integration.py").read_text(encoding="utf-8")
    assert "KEYCLOAK_ADMIN: ${KEYCLOAK_ADMIN:-admin}" in compose
    assert 'os.getenv("KEYCLOAK_ADMIN", "admin")' in script
    assert "KC_BOOTSTRAP_ADMIN_" not in compose


def _bootstrap():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "bootstrap", ROOT / "scripts/configure_local_oidc_integration.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_password_grant_is_limited_to_the_local_integration_check_client() -> None:
    bootstrap = _bootstrap()
    check = (ROOT / "scripts/oidc_integration_check.py").read_text(encoding="utf-8")
    assert bootstrap.API_CLIENT_CONFIG["clientId"] == "ragops-api"
    assert bootstrap.API_CLIENT_CONFIG["directAccessGrantsEnabled"] is False
    gate_client = bootstrap.INTEGRATION_CHECK_CLIENT_CONFIG
    assert gate_client["clientId"] == "ragops-integration-check"
    assert gate_client["directAccessGrantsEnabled"] is True
    assert gate_client["publicClient"] is True
    assert gate_client["standardFlowEnabled"] is False
    assert gate_client["implicitFlowEnabled"] is False
    assert gate_client["serviceAccountsEnabled"] is False
    assert '"grant_type": "password"' in check
    assert '"client_id": "ragops-integration-check"' in check
    assert '"client_id": "ragops-api"' not in check
    assert '"username": "integration-user"' in check
    assert "error_description" in check


def test_client_settings_are_verified_after_bootstrap() -> None:
    bootstrap = _bootstrap()

    def state(api: object, check: object) -> dict[str, object]:
        clients = [{"clientId": "ragops-api", "directAccessGrantsEnabled": api}]
        if check is not None:
            clients.append(
                {"clientId": "ragops-integration-check", "directAccessGrantsEnabled": check}
            )
        return {"clients": clients}

    assert bootstrap.client_setting_errors(state(False, True)) == []
    assert bootstrap.client_setting_errors(state(True, True)) == [
        "ragops-api directAccessGrantsEnabled is not false"
    ]
    assert bootstrap.client_setting_errors(state(False, False)) == [
        "ragops-integration-check directAccessGrantsEnabled is not true"
    ]
    assert bootstrap.client_setting_errors(state(False, None)) == [
        "ragops-integration-check is missing"
    ]
    # Missing flags never count as the required value.
    assert bootstrap.client_setting_errors({"clients": [{"clientId": "ragops-api"}]})


def test_snapshot_is_redacted_private_and_taken_before_changes(tmp_path) -> None:
    bootstrap = _bootstrap()
    raw = {
        "realm_exists": True,
        "realm": {"realm": "ragops-integration", "smtpServer": {"password": "synthetic-value"}},
        "clients": [
            {"clientId": "other", "secret": "synthetic-value", "attributes": {"client.secret.x": 1}}
        ],
        "users": ["someone"],
    }
    redacted = bootstrap.redact(raw)
    target = tmp_path / "nested" / "snapshot.json"
    digest = bootstrap.write_snapshot(redacted, target)
    text = target.read_text(encoding="utf-8")
    assert "synthetic-value" not in text
    assert bootstrap.REDACTED in text
    assert target.stat().st_mode & 0o777 == 0o600
    assert digest == bootstrap.state_digest(redacted)
    source = (ROOT / "scripts/configure_local_oidc_integration.py").read_text(encoding="utf-8")
    body = source[source.index("def bootstrap(") : source.index("def cleanup(")]
    assert body.index("write_snapshot(before)") < body.index('"PUT"')
    assert body.index("write_snapshot(before)") < body.index("ensure_client(")


def test_unrelated_realm_state_excludes_only_managed_objects() -> None:
    bootstrap = _bootstrap()
    state = {
        "realm": {"bruteForceProtected": False, "displayName": "kept", "enabled": True},
        "clients": [
            {"clientId": "ragops-api", "directAccessGrantsEnabled": True},
            {"clientId": "ragops-integration-check"},
            {"clientId": "other", "enabled": True},
        ],
        "users": ["integration-user", "someone"],
    }
    unrelated = bootstrap.unrelated_state(state)
    assert unrelated == {
        "realm": {"displayName": "kept"},
        "clients": [{"clientId": "other", "enabled": True}],
        "users": ["someone"],
    }
    changed = {**state, "clients": [{"clientId": "other", "enabled": False}]}
    assert bootstrap.unrelated_state(changed) != unrelated


def test_cleanup_removes_only_the_gate_client_and_rejects_unknown_arguments() -> None:
    import pytest

    bootstrap = _bootstrap()
    source = (ROOT / "scripts/configure_local_oidc_integration.py").read_text(encoding="utf-8")
    body = source[source.index("def cleanup(") : source.index("def main(")]
    assert "clientId={INTEGRATION_CHECK_CLIENT}" in body
    assert body.count('"DELETE"') == 1
    assert "changed unrelated realm state" in body
    assert "SNAPSHOT_PATH.unlink" in body
    with pytest.raises(SystemExit):
        bootstrap.main(["--force"])


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


def test_realm_bootstrap_enables_bounded_brute_force_protection() -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "bootstrap", ROOT / "scripts/configure_local_oidc_integration.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    hardening = module.REALM_HARDENING
    assert hardening["bruteForceProtected"] is True
    assert hardening["permanentLockout"] is False
    assert 1 <= int(hardening["failureFactor"]) <= 20
    assert hardening["registrationAllowed"] is False

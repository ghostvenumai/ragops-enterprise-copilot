"""Security release gate: an evidence orchestrator over existing controls.

The gate adds no probes of its own. For every mandatory control of the threat model
(``docs/THREAT_MODEL.md``) it collects the evidence that already exists:

* ``regression`` - the named regression tests, run once, or a benign read-only check of
  repository configuration;
* ``live`` - the sanitized evidence another RC gate wrote for the tested commit, or a
  read-only look at the local Keycloak realm settings.

A control counts only when its evidence is present, well-formed, current and true.
Missing, malformed or stale evidence keeps the gate BLOCKED; evidence that shows a
control failing, a failed cleanup or evidence that would leak a secret is a FAIL.
"""
# ruff: noqa: S603

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

RC_LIVE = ROOT / "evidence" / "product-v1" / "rc-live"
EVIDENCE = RC_LIVE / "security.json"
WORK_PARENT = ROOT / ".loop" / "tmp"
MAX_EVIDENCE_AGE = timedelta(hours=24)
CLOCK_SKEW = timedelta(minutes=5)
REGRESSION_TIMEOUT_SECONDS = 900
GATE_ONLY_CLIENT = "ragops-integration-check"
# Paths whose uncommitted changes would make "tested_commit" untrue.
PRODUCT_PATHS = (
    "src",
    "apps",
    "scripts",
    "tests",
    "deploy",
    "migrations",
    "docker-compose.yml",
    "docker-compose.prod.yml",
    "docker-compose.integration.yml",
    "Dockerfile",
    "pyproject.toml",
    "constraints.txt",
)
# Regression evidence must not depend on the operator's sourced integration environment.
INTEGRATION_ENV_PREFIXES = ("RAGOPS_", "KEYCLOAK_", "OPENAI_", "AZURE_OPENAI_")
SECRET_PATTERNS = (
    re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\."),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"[a-z][a-z0-9+.-]*://[^/\s:@]+:[^@\s]+@"),
)
SECRET_ENV_NAME = re.compile(r"PASSWORD|SECRET|API_KEY|TOKEN|DATABASE_URL")

PASS, FAIL, MISSING, MALFORMED, STALE = "PASS", "FAIL", "MISSING", "MALFORMED", "STALE"


@dataclass(frozen=True)
class Control:
    """One mandatory control and the single place its evidence comes from."""

    id: str
    stride: str
    title: str
    kind: str  # "regression" or "live"
    tests: tuple[str, ...] = ()
    config_check: str = ""
    evidence_file: str = ""
    required: tuple[tuple[str, object], ...] = ()
    keycloak: bool = False

    def evidence_source(self) -> str:
        if self.tests:
            return "<br>".join(f"`{test}`" for test in self.tests)
        if self.config_check:
            return f"config check `{self.config_check}` (`scripts/security_gate.py`)"
        if self.keycloak:
            return "read-only Keycloak realm settings (`scripts/security_gate.py`)"
        fields = ", ".join(f"`{key}={json.dumps(value)}`" for key, value in self.required)
        return f"`evidence/product-v1/rc-live/{self.evidence_file}`: {fields}"


@dataclass(frozen=True)
class ControlResult:
    id: str
    status: str
    detail: str


_IDENTITY = "tests/security/test_identity.py"
_CONFUSION = "tests/security/test_token_type_confusion.py"
_ADMIN = "tests/security/test_admin_tenant_scope.py"
_CATALOG = "tests/security/test_catalog_platform_admin.py"
_FOUNDATION = "tests/security/test_product_foundation.py"
_UPLOAD_API = "tests/security/test_ingestion_upload_api.py"
_HTTP = "tests/security/test_http_hardening.py"
_RATE_API = "tests/security/test_rate_limit_api.py"
_CONTROLS = "tests/unit/test_security_controls.py"
_INJECTION = "tests/unit/test_prompt_injection.py"
_INSPECTION = "tests/unit/test_document_inspection.py"
_SIGNATURES = "tests/unit/test_upload_signatures.py"
_RETRIEVAL = "tests/unit/test_ingestion_retrieval.py"
_VECTOR = "tests/product/test_vector_index.py"
_BACKUP = "tests/product/test_data_backup.py"
_BACKUP_KEY = "tests/product/test_backup_key_bytes.py"
_RATE = "tests/product/test_rate_limit.py"
_KEYCLOAK = "tests/product/test_keycloak_bootstrap_config.py"

CONTROLS: tuple[Control, ...] = (
    Control(
        "SEC-01",
        "Spoofing",
        "Bearer tokens are verified (signature, issuer, audience, claims); client-supplied "
        "identity is ignored",
        "regression",
        tests=(
            f"{_IDENTITY}::test_invalid_claims_are_rejected",
            f"{_IDENTITY}::test_invalid_signature_and_malformed_token_rejected",
            f"{_IDENTITY}::test_forged_admin_role_requires_valid_signature",
            f"{_IDENTITY}::test_dependency_returns_401_without_bearer",
            f"{_IDENTITY}::test_dependency_rejects_invalid_bearer",
            f"{_IDENTITY}::test_client_identity_override_is_ignored_for_oidc",
        ),
    ),
    Control(
        "SEC-02",
        "Spoofing",
        "Only access tokens of approved clients are accepted (token-type confusion)",
        "regression",
        tests=(
            f"{_CONFUSION}::test_non_access_token_types_are_rejected",
            f"{_CONFUSION}::test_access_tokens_are_accepted",
            f"{_CONFUSION}::test_logout_header_type_is_rejected",
            f"{_CONFUSION}::test_authorized_party_allowlist_is_enforced_when_configured",
            f"{_CONFUSION}::test_unknown_critical_header_extension_is_rejected",
        ),
    ),
    Control(
        "SEC-03",
        "Spoofing",
        "The product client has no password grant; the realm bounds login attempts",
        "regression",
        tests=(
            f"{_KEYCLOAK}::test_password_grant_is_limited_to_the_local_integration_check_client",
            f"{_KEYCLOAK}::test_client_settings_are_verified_after_bootstrap",
            f"{_KEYCLOAK}::test_realm_bootstrap_enables_bounded_brute_force_protection",
            f"{_KEYCLOAK}::test_cleanup_removes_only_the_gate_client_and_rejects_unknown_arguments",
        ),
    ),
    Control(
        "SEC-04",
        "Information disclosure",
        "Retrieval and vector queries are tenant- and access-filtered before scoring",
        "regression",
        tests=(
            "tests/security/test_mandantentrennung.py::test_cross_tenant_retrieval_is_blocked",
            f"{_RETRIEVAL}::test_hybrid_search_enforces_tenant_and_access_filters",
            f"{_VECTOR}::test_filter_enforces_tenant_scope_access_lifecycle_and_validity",
            f"{_VECTOR}::test_workspace_and_collection_scope_cannot_be_broadened",
        ),
    ),
    Control(
        "SEC-05",
        "Elevation of privilege",
        "Administration requires the admin role and stays inside the admin's tenant",
        "regression",
        tests=(
            f"{_ADMIN}::test_model_policy_listing_is_scoped_to_admin_tenant",
            f"{_ADMIN}::test_audit_events_are_scoped_to_admin_tenant",
            f"{_ADMIN}::test_non_admin_cannot_read_admin_tenant_views",
            f"{_FOUNDATION}::test_admin_is_always_scoped_to_own_tenant",
            f"{_IDENTITY}::test_admin_dependency_rejects_non_admin_claim",
            f"{_CONTROLS}::test_rbac_blocks_restricted_for_sales",
        ),
    ),
    Control(
        "SEC-06",
        "Elevation of privilege",
        "Only platform-tenant admins change the shared model catalog; unset fails closed",
        "regression",
        tests=(
            f"{_CATALOG}::test_tenant_admin_cannot_change_the_shared_catalog",
            f"{_CATALOG}::test_platform_admin_can_change_the_catalog",
            f"{_CATALOG}::test_catalog_changes_fail_closed_without_a_platform_tenant",
            "tests/security/test_model_router_api.py::test_non_admin_cannot_simulate_or_change_routing",
        ),
    ),
    Control(
        "SEC-07",
        "Tampering",
        "Uploads are validated by name, path, signature and structure before storage",
        "regression",
        tests=(
            f"{_CONTROLS}::test_safe_filename_blocks_path_traversal",
            f"{_CONTROLS}::test_validate_document_path_blocks_escape",
            f"{_FOUNDATION}::test_hostile_upload_names_rejected",
            f"{_SIGNATURES}::test_real_pdf_and_docx_signatures_are_accepted",
            f"{_SIGNATURES}::test_mismatched_content_is_rejected",
            f"{_INSPECTION}::test_real_documents_are_accepted",
            f"{_INSPECTION}::test_unsafe_or_malformed_pdfs_are_rejected",
            f"{_INSPECTION}::test_unsafe_or_malformed_docx_archives_are_rejected",
            f"{_INSPECTION}::test_text_uploads_must_be_clean_utf8",
            f"{_UPLOAD_API}::test_renamed_file_is_rejected_before_a_job_exists",
        ),
    ),
    Control(
        "SEC-08",
        "Elevation of privilege",
        "Instruction-like text in questions and documents is detected and never followed",
        "regression",
        tests=(
            f"{_INJECTION}::test_direct_injection_phrases_are_detected",
            f"{_INJECTION}::test_benign_text_is_not_flagged",
            f"{_INJECTION}::test_zero_width_and_invisible_characters_do_not_bypass_detection",
            f"{_INJECTION}::test_bidi_override_characters_are_stripped_before_matching",
            f"{_INJECTION}::test_letter_spacing_evasion_is_caught_by_compact_matching",
            f"{_INJECTION}::test_hidden_instruction_inside_html_comment_is_detected",
            f"{_INJECTION}::test_base64_encoded_instruction_is_decoded_and_detected",
            f"{_CONTROLS}::test_prompt_injection_patterns_are_detected",
            f"{_RETRIEVAL}::test_ingestion_extracts_metadata_and_prompt_injection_flags",
            "tests/integration/test_workflow.py::test_workflow_blocks_user_prompt_injection",
        ),
    ),
    Control(
        "SEC-09",
        "Repudiation",
        "Requests carry a correlation ID into audit events; PII is masked before storage",
        "regression",
        tests=(
            "tests/integration/test_api.py::test_query_validation_metrics_and_audit",
            f"{_CONTROLS}::test_pii_masking_redacts_email_and_phone",
        ),
    ),
    Control(
        "SEC-10",
        "Denial of service",
        "Oversized bodies are rejected before parsing; malformed input never causes a 5xx",
        "regression",
        tests=(
            f"{_HTTP}::test_every_response_carries_security_headers",
            f"{_HTTP}::test_oversized_body_is_rejected_before_parsing",
            f"{_HTTP}::test_malformed_numbers_never_cause_server_errors",
            f"{_HTTP}::test_budget_input_is_validated",
            f"{_UPLOAD_API}::test_malformed_identifiers_are_not_found_not_server_errors",
        ),
    ),
    Control(
        "SEC-11",
        "Denial of service",
        "Every authenticated route is rate limited; a limiter outage fails closed",
        "regression",
        tests=(
            f"{_RATE_API}::test_query_route_enforces_limit_before_provider",
            f"{_RATE_API}::test_redis_outage_fails_closed_with_503",
            f"{_RATE_API}::test_every_authenticated_route_is_limited_and_exemptions_are_narrow",
            f"{_RATE}::test_backend_failures_raise_unavailable_not_allow",
            f"{_RATE}::test_redis_keys_are_pseudonymized_and_collision_free",
        ),
    ),
    Control(
        "SEC-12",
        "Information disclosure",
        "Backups are encrypted with an external private key; tampered sets are rejected",
        "regression",
        tests=(
            f"{_BACKUP}::test_artifact_is_encrypted_and_key_is_external",
            f"{_BACKUP}::test_corrupt_or_unauthenticated_artifacts_fail_before_target_mutation",
            f"{_BACKUP}::test_error_messages_never_contain_plaintext",
            f"{_BACKUP}::test_key_file_must_be_private_and_sized",
            f"{_BACKUP_KEY}::test_raw_keys_with_whitespace_boundary_bytes_are_preserved",
            f"{_BACKUP_KEY}::test_raw_key_with_a_trailing_newline_is_rejected_not_truncated",
        ),
    ),
    Control(
        "SEC-13",
        "Elevation of privilege",
        "Production configuration fails closed (no demo runtime, no provider fallback)",
        "regression",
        tests=(
            f"{_FOUNDATION}::test_production_cannot_start_demo_runtime",
            f"{_FOUNDATION}::test_invalid_environment_fails_closed",
            f"{_FOUNDATION}::test_provider_typo_does_not_fallback",
            f"{_IDENTITY}::test_development_provider_is_environment_bounded",
            f"{_IDENTITY}::test_oidc_configuration_fails_closed",
            f"{_VECTOR}::test_production_vector_configuration_fails_closed",
        ),
    ),
    Control(
        "CFG-01",
        "Information disclosure",
        "The TLS edge sets security headers, hides the server and marks cookies Secure",
        "regression",
        config_check="edge_proxy_headers",
    ),
    Control(
        "CFG-02",
        "Elevation of privilege",
        "The edge container is read-only, drops capabilities and forbids new privileges; "
        "production selects OIDC",
        "regression",
        config_check="production_compose_hardening",
    ),
    Control(
        "CFG-03",
        "Tampering",
        "Direct dependencies are pinned and mirrored in constraints.txt",
        "regression",
        config_check="dependency_pins",
    ),
    Control(
        "CFG-04",
        "Information disclosure",
        "No environment, key or secret file is tracked in Git",
        "regression",
        config_check="secret_files_untracked",
    ),
    Control(
        "LIVE-01",
        "Spoofing",
        "A real Keycloak token is validated; a wrong audience is rejected",
        "live",
        evidence_file="oidc.json",
        required=(("status", "PASS"),),
    ),
    Control(
        "LIVE-02",
        "Spoofing",
        "The running realm has no password grant on ragops-api, bounds login attempts and "
        "disables self-registration",
        "live",
        keycloak=True,
    ),
    Control(
        "LIVE-03",
        "Information disclosure",
        "Tenant isolation holds across persistence, API, ingestion, vectors and FinOps",
        "live",
        evidence_file="tenant-isolation.json",
        required=(
            ("status", "PASS"),
            ("tenant_leakage", 0),
            ("vector_tenant_leakage", 0),
            ("unauthorized_access_count", 0),
            ("cleanup_status", "PASS"),
        ),
    ),
    Control(
        "LIVE-04",
        "Elevation of privilege",
        "Tenant admins cannot influence other tenants through the shared model catalog",
        "live",
        evidence_file="model-router.json",
        required=(
            ("status", "PASS"),
            ("router_tenant_leakage", 0),
            ("cross_tenant_catalog_influence", False),
            ("paid_provider_calls", 0),
        ),
    ),
    Control(
        "LIVE-05",
        "Denial of service",
        "Distributed rate limiting is atomic, tenant-isolated and fails closed on Redis",
        "live",
        evidence_file="rate-limiting.json",
        required=(
            ("status", "PASS"),
            ("atomicity_verified", True),
            ("redis_outage_verified", True),
            ("spoofed_identity_rejected", True),
            ("rate_limit_tenant_leakage", 0),
            ("cleanup_status", "PASS"),
        ),
    ),
    Control(
        "LIVE-06",
        "Information disclosure",
        "Encrypted backup and restore reject wrong keys, tampering and unsafe targets",
        "live",
        evidence_file="backup-restore.json",
        required=(
            ("status", "PASS"),
            ("encryption_enabled", True),
            ("key_external_to_artifact", True),
            ("wrong_key_rejected", True),
            ("tamper_rejected", True),
            ("backup_restore_tenant_leakage", 0),
            ("secret_scan_passed", True),
            ("cleanup_status", "PASS"),
        ),
    ),
    Control(
        "LIVE-07",
        "Denial of service",
        "Dependency outages turn the instance unready with sanitized reasons and no leakage",
        "live",
        evidence_file="readiness-failure-recovery.json",
        required=(
            ("status", "PASS"),
            ("readiness_payload_sanitized", True),
            ("rate_limit_fail_closed", True),
            ("readiness_recovery_tenant_leakage", 0),
            ("secret_scan_passed", True),
            ("cleanup_status", "PASS"),
        ),
    ),
    Control(
        "LIVE-08",
        "Spoofing",
        "Browser login, logout, session expiry, role denial and tenant isolation hold end to end",
        "live",
        evidence_file="browser-e2e.json",
        required=(
            ("status", "PASS"),
            ("real_browser_login_verified", True),
            ("logout_verified", True),
            ("session_expiry_verified", True),
            ("backend_rbac_denied", True),
            ("browser_e2e_tenant_leakage", 0),
            ("sensitive_artifacts_detected", False),
            ("secret_scan_passed", True),
            ("cleanup_status", "PASS"),
        ),
    ),
    Control(
        "LIVE-09",
        "Tampering",
        "The production Compose configuration is valid on the host",
        "live",
        evidence_file="docker-compose.json",
        required=(("status", "PASS"),),
    ),
)


# --------------------------------------------------------------------------- regression


def expected_tests() -> list[str]:
    """Every named regression test, once, in matrix order."""
    return list(dict.fromkeys(test for control in CONTROLS for test in control.tests))


def regression_environment(environment: Mapping[str, str]) -> dict[str, str]:
    """The host's integration settings and credentials never reach the regression tests."""
    return {
        name: value
        for name, value in environment.items()
        if not name.startswith(INTEGRATION_ENV_PREFIXES)
    }


def run_regression(work: Path) -> Path:
    """Run the named tests once; the JUnit report is the only thing read from the run."""
    report = work / "regression.xml"
    command = [
        sys.executable,
        "-m",
        "pytest",
        *expected_tests(),
        "-q",
        "-p",
        "no:cacheprovider",
        f"--junitxml={report}",
    ]
    try:
        subprocess.run(
            command,
            cwd=ROOT,
            env=regression_environment(os.environ),
            capture_output=True,
            text=True,
            check=False,
            timeout=REGRESSION_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        report.unlink(missing_ok=True)
    return report


def parse_junit(report: Path) -> dict[str, list[str]] | None:
    """Outcomes per ``file::function``; None when the report is missing or malformed."""
    from defusedxml import DefusedXmlException
    from defusedxml.ElementTree import ParseError, parse

    try:
        root = parse(report).getroot()
    except (OSError, ParseError, DefusedXmlException, ValueError):
        return None
    outcomes: dict[str, list[str]] = {}
    for case in root.iter("testcase"):
        classname, name = case.get("classname"), case.get("name")
        if not classname or not name:
            return None
        node = f"{classname.replace('.', '/')}.py::{name.split('[', 1)[0]}"
        children = {child.tag for child in case}
        outcome = (
            "failed"
            if children & {"failure", "error"}
            else "skipped"
            if "skipped" in children
            else "passed"
        )
        outcomes.setdefault(node, []).append(outcome)
    return outcomes


def evaluate_regression(control: Control, outcomes: dict[str, list[str]] | None) -> ControlResult:
    if outcomes is None:
        return ControlResult(control.id, MALFORMED, "regression report missing or unreadable")
    executed = 0
    for test in control.tests:
        results = outcomes.get(test, [])
        if "failed" in results:
            return ControlResult(control.id, FAIL, f"{test} failed")
        if not results or "skipped" in results:
            return ControlResult(control.id, MISSING, f"{test} did not run")
        executed += len(results)
    return ControlResult(control.id, PASS, f"{executed} test cases passed")


# --------------------------------------------------------------------------- config checks


def _read(root: Path, name: str) -> str:
    return (root / name).read_text(encoding="utf-8")


def check_edge_proxy_headers(root: Path) -> tuple[bool, str]:
    caddyfile = _read(root, "deploy/Caddyfile")
    required = (
        "-Server",
        "X-Content-Type-Options nosniff",
        "X-Frame-Options DENY",
        "Referrer-Policy no-referrer",
        'Strict-Transport-Security "max-age=31536000',
        "frame-ancestors 'none'",
        'header_down Set-Cookie "^(.*)$" "$1; Secure"',
    )
    missing = [item for item in required if item not in caddyfile]
    return not missing, f"missing: {missing[0]}" if missing else f"{len(required)} directives"


def _compose_service(compose: str, service: str) -> str:
    match = re.search(rf"^  {re.escape(service)}:\n((?:    .*\n|\n)*)", compose, re.MULTILINE)
    return match.group(1) if match else ""


def check_production_compose_hardening(root: Path) -> tuple[bool, str]:
    compose = _read(root, "docker-compose.prod.yml")
    proxy, api = _compose_service(compose, "reverse-proxy"), _compose_service(compose, "api")
    required = {
        "edge read_only": "read_only: true" in proxy,
        "edge no-new-privileges": "- no-new-privileges:true" in proxy,
        "edge cap_drop ALL": bool(re.search(r"cap_drop:\n\s+- ALL\n", proxy)),
        "edge only NET_BIND_SERVICE": bool(
            re.search(r"cap_add:\n\s+- NET_BIND_SERVICE\n(?!\s+- )", proxy)
        ),
        "edge Caddyfile read-only": "./deploy/Caddyfile:/etc/caddy/Caddyfile:ro" in proxy,
        "api production": "RAGOPS_ENV: production" in api,
        "api oidc": "RAGOPS_IDENTITY_PROVIDER: oidc" in api,
    }
    failed = [name for name, ok in required.items() if not ok]
    return not failed, f"not satisfied: {failed[0]}" if failed else f"{len(required)} settings"


def _normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def check_dependency_pins(root: Path) -> tuple[bool, str]:
    project = tomllib.loads(_read(root, "pyproject.toml"))["project"]
    requirements = [
        *project.get("dependencies", []),
        *(item for group in project.get("optional-dependencies", {}).values() for item in group),
    ]
    constraints = {}
    for line in _read(root, "constraints.txt").splitlines():
        if "==" in line and not line.lstrip().startswith("#"):
            name, version = line.split("==", 1)
            constraints[_normalized(name.strip())] = version.split(";")[0].strip()
    for requirement in requirements:
        match = re.fullmatch(r"([A-Za-z0-9_.-]+)(?:\[[^\]]+\])?==([^\s;]+)", requirement.strip())
        if not match:
            return False, f"not pinned: {requirement}"
        if constraints.get(_normalized(match.group(1))) != match.group(2):
            return False, f"not mirrored in constraints.txt: {requirement}"
    return bool(requirements), f"{len(requirements)} pinned requirements mirrored"


def check_secret_files_untracked(root: Path) -> tuple[bool, str]:
    tracked = subprocess.run(
        ["git", "ls-files"],  # noqa: S607
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout.splitlines()
    allowed = {".env.example", ".env.integration.example"}
    offending = [
        path
        for path in tracked
        if Path(path).name not in allowed
        and (
            Path(path).name == ".env"
            or Path(path).name.startswith(".env.")
            or Path(path).suffix in {".secret", ".pem", ".key", ".p12", ".pfx"}
        )
    ]
    ignore = _read(root, ".gitignore").splitlines()
    if ".env" not in ignore or ".env.*" not in ignore:
        return False, ".env files are not ignored"
    return (
        not offending,
        f"tracked: {offending[0]}" if offending else f"{len(tracked)} tracked files",
    )


CONFIG_CHECKS: dict[str, Callable[[Path], tuple[bool, str]]] = {
    "edge_proxy_headers": check_edge_proxy_headers,
    "production_compose_hardening": check_production_compose_hardening,
    "dependency_pins": check_dependency_pins,
    "secret_files_untracked": check_secret_files_untracked,
}


def evaluate_config(control: Control, root: Path = ROOT) -> ControlResult:
    check = CONFIG_CHECKS.get(control.config_check)
    if check is None:
        return ControlResult(control.id, MISSING, "unknown config check")
    try:
        ok, detail = check(root)
    except FileNotFoundError as exc:
        return ControlResult(control.id, MISSING, f"{Path(str(exc.filename)).name} not found")
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        return ControlResult(control.id, MALFORMED, type(exc).__name__)
    return ControlResult(control.id, PASS if ok else FAIL, detail)


# --------------------------------------------------------------------------- live evidence


def evaluate_live_evidence(
    control: Control, directory: Path, commit: str, now: datetime
) -> ControlResult:
    """Evidence of another gate counts only for this commit, recently, and when true."""
    path = directory / control.evidence_file
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return ControlResult(control.id, MISSING, f"{control.evidence_file} not found")
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError
        tested = data["tested_commit"]
        timestamp = datetime.fromisoformat(data["timestamp"])
        if not isinstance(tested, str) or timestamp.tzinfo is None:
            raise ValueError
    except (ValueError, KeyError, TypeError):
        return ControlResult(control.id, MALFORMED, f"{control.evidence_file} is malformed")
    if not commit or tested != commit:
        return ControlResult(control.id, STALE, f"{control.evidence_file} is for another commit")
    if not -CLOCK_SKEW <= now - timestamp <= MAX_EVIDENCE_AGE:
        return ControlResult(control.id, STALE, f"{control.evidence_file} is outdated")
    for key, expected in control.required:
        if key not in data:
            return ControlResult(control.id, MISSING, f"{control.evidence_file} lacks {key}")
        actual = data[key]
        # Exact type as well: True is not 1 and "0" is not 0.
        if type(actual) is not type(expected) or actual != expected:
            if key == "status" and actual == "BLOCKED":
                return ControlResult(control.id, MISSING, f"{control.evidence_file} is BLOCKED")
            return ControlResult(control.id, FAIL, f"{control.evidence_file}: {key} is not met")
    return ControlResult(control.id, PASS, f"{len(control.required)} fields verified")


def read_keycloak_state() -> tuple[dict[str, Any] | None, tuple[str, ...]]:
    """Redacted realm settings through the local admin API, plus the secrets used."""
    import urllib.error

    from scripts import configure_local_oidc_integration as bootstrap
    from scripts.browser_e2e_gate import keycloak_admin_credentials

    credentials = keycloak_admin_credentials()
    if credentials is None:
        return None, ()
    try:
        token = bootstrap.admin_token(bootstrap.BASE, credentials)
        return bootstrap.realm_state(bootstrap.BASE, token), (credentials[1], token)
    except (SystemExit, OSError, urllib.error.URLError, ValueError, KeyError):
        return None, (credentials[1],)


def evaluate_keycloak(control: Control, state: dict[str, Any] | None) -> ControlResult:
    if state is None or not state.get("realm_exists"):
        return ControlResult(control.id, MISSING, "Keycloak realm settings are unavailable")
    try:
        realm = state["realm"]
        clients = {str(client["clientId"]): client for client in state["clients"]}
        if not isinstance(realm, dict):
            raise TypeError
    except (KeyError, TypeError):
        return ControlResult(control.id, MALFORMED, "Keycloak realm settings are malformed")
    api = clients.get("ragops-api")
    if api is None:
        return ControlResult(control.id, MISSING, "ragops-api client not found")
    required = {
        "ragops-api directAccessGrantsEnabled=false": api.get("directAccessGrantsEnabled") is False,
        "bruteForceProtected=true": realm.get("bruteForceProtected") is True,
        "permanentLockout=false": realm.get("permanentLockout") is False,
        "registrationAllowed=false": realm.get("registrationAllowed") is False,
    }
    failed = [name for name, ok in required.items() if not ok]
    if failed:
        return ControlResult(control.id, FAIL, f"not satisfied: {failed[0]}")
    return ControlResult(control.id, PASS, f"{len(required)} realm settings verified")


def gate_only_client_present(state: dict[str, Any] | None) -> bool:
    clients = state.get("clients", []) if isinstance(state, dict) else []
    return any(
        isinstance(client, dict) and client.get("clientId") == GATE_ONLY_CLIENT
        for client in clients
    )


# --------------------------------------------------------------------------- aggregation


def classify(
    results: list[ControlResult], cleanup_ok: bool, worktree_clean: bool
) -> tuple[str, str]:
    by_id = {result.id: result for result in results}
    failed = [result for result in results if result.status == FAIL]
    if failed:
        return FAIL, f"mandatory control {failed[0].id} failed: {failed[0].detail}"
    if not cleanup_ok:
        return FAIL, "gate cleanup failed: gate-only objects remain"
    unevidenced = [
        control.id
        for control in CONTROLS
        if control.id not in by_id or by_id[control.id].status != PASS
    ]
    if unevidenced or len(results) != len(CONTROLS):
        return (
            "BLOCKED",
            f"{len(unevidenced)} of {len(CONTROLS)} mandatory controls lack admissible "
            f"evidence (first: {unevidenced[0] if unevidenced else 'unknown control'})",
        )
    if not worktree_clean:
        return "BLOCKED", "uncommitted product changes: evidence is not attributable to a commit"
    return PASS, f"all {len(CONTROLS)} mandatory controls are evidenced"


def leaks(serialized: str, secrets: tuple[str, ...]) -> bool:
    return any(secret and secret in serialized for secret in secrets) or any(
        pattern.search(serialized) for pattern in SECRET_PATTERNS
    )


def environment_secrets() -> tuple[str, ...]:
    return tuple(
        value
        for name, value in os.environ.items()
        if SECRET_ENV_NAME.search(name) and len(value) >= 8
    )


def _finish(evidence: dict[str, Any], status: str, reason: str, secrets: tuple[str, ...]) -> int:
    exit_code = {PASS: 0, "BLOCKED": 2}.get(status, 1)
    evidence.update(
        {
            "status": status,
            "reason": reason,
            "exit_code": exit_code,
            "secret_scan_passed": True,
            "timestamp": datetime.now(UTC).isoformat(),
        }
    )
    serialized = json.dumps(evidence, indent=2) + "\n"
    if leaks(serialized, secrets):
        evidence.update(
            {
                "status": FAIL,
                "reason": "evidence redaction failed",
                "exit_code": 1,
                "secret_scan_passed": False,
            }
        )
        keep = ("gate", "status", "reason", "exit_code", "secret_scan_passed", "tested_commit")
        serialized = (
            json.dumps({key: evidence.get(key) for key in (*keep, "timestamp")}, indent=2) + "\n"
        )
        exit_code = 1
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(serialized, encoding="utf-8")
    return exit_code


def _git(*arguments: str) -> str | None:
    try:
        completed = subprocess.run(
            ["git", *arguments],  # noqa: S607
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout if completed.returncode == 0 else None


def tested_commit() -> str:
    return (_git("rev-parse", "HEAD") or "").strip()


def worktree_is_clean() -> bool:
    status = _git("status", "--porcelain", "--", *PRODUCT_PATHS)
    return status is not None and not status.strip()


def matrix_markdown() -> str:
    lines = [
        "| ID | STRIDE | Mandatory control | Type | Evidence source |",
        "| --- | --- | --- | --- | --- |",
    ]
    lines += [
        f"| {control.id} | {control.stride} | {control.title} | {control.kind} | "
        f"{control.evidence_source()} |"
        for control in CONTROLS
    ]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    if arguments == ["--matrix"]:
        sys.stdout.write(matrix_markdown())
        return 0
    commit = tested_commit()
    evidence: dict[str, Any] = {"gate": "security", "tested_commit": commit}
    WORK_PARENT.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="security-gate-", dir=WORK_PARENT))
    try:
        outcomes = parse_junit(run_regression(work))
    finally:
        shutil.rmtree(work, ignore_errors=True)
    keycloak_state, keycloak_secrets = read_keycloak_state()
    now = datetime.now(UTC)
    results = []
    for control in CONTROLS:
        if control.tests:
            results.append(evaluate_regression(control, outcomes))
        elif control.config_check:
            results.append(evaluate_config(control))
        elif control.keycloak:
            results.append(evaluate_keycloak(control, keycloak_state))
        else:
            results.append(evaluate_live_evidence(control, RC_LIVE, commit, now))
    by_id = {result.id: result for result in results}
    leftovers = [
        name
        for name, present in (
            ("regression work directory", work.exists()),
            (f"Keycloak client {GATE_ONLY_CLIENT}", gate_only_client_present(keycloak_state)),
        )
        if present
    ]
    clean = worktree_is_clean()
    status, reason = classify(results, not leftovers, clean)
    named = expected_tests()
    evidence.update(
        {
            "worktree_clean": clean,
            "mandatory_control_count": len(CONTROLS),
            "evidenced_control_count": sum(result.status == PASS for result in results),
            "regression_control_count": sum(c.kind == "regression" for c in CONTROLS),
            "live_control_count": sum(c.kind == "live" for c in CONTROLS),
            "regression_tests_named": len(named),
            "regression_tests_passed": sum(
                1 for test in named if set((outcomes or {}).get(test, [])) == {"passed"}
            ),
            "max_evidence_age_hours": int(MAX_EVIDENCE_AGE.total_seconds() // 3600),
            "cleanup_status": "FAIL" if leftovers else PASS,
            "gate_only_objects_remaining": leftovers,
            "controls": [
                {
                    "id": control.id,
                    "stride": control.stride,
                    "control": control.title,
                    "type": control.kind,
                    "evidence_source": control.evidence_source().replace("<br>", ", "),
                    "status": by_id[control.id].status,
                    "detail": by_id[control.id].detail,
                }
                for control in CONTROLS
            ],
        }
    )
    return _finish(evidence, status, reason, (*environment_secrets(), *keycloak_secrets))


if __name__ == "__main__":
    raise SystemExit(main())

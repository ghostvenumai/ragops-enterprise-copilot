"""Browser end-to-end RC gate: a real headless Chromium drives the live Streamlit dashboard.

The browser follows the real redirect to the local Keycloak realm, types into its login form
and returns to the product. The dashboard, API and worker run as real processes on
gate-owned loopback ports against a gate-owned ``ragops_test_e2e_<run>`` database, a
gate-owned Redis namespace and a disposable RC Qdrant service; only the model provider is
the deterministic local provider, watched by a ledger that fails any paid construction.
Keycloak users and the dashboard client are created for this run and removed afterwards.
Evidence and screenshots are written below ``evidence/product-v1/rc-live/`` and scanned for
credentials before they are kept.
"""
# ruff: noqa: S310, S603, S607

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.fault_proxy import FaultProxy  # noqa: E402

RC_LIVE = ROOT / "evidence" / "product-v1" / "rc-live"
EVIDENCE = RC_LIVE / "browser-e2e.json"
ARTIFACTS = RC_LIVE / "browser-e2e"
GATE_DATABASE = re.compile(r"^ragops_test_e2e_[0-9a-f]{10}$")
KEYCLOAK_URL = os.getenv("RAGOPS_E2E_KEYCLOAK_URL", "http://localhost:8081").rstrip("/")
REALM = "ragops-integration"
API_CLIENT = "ragops-api"
ROLES_CLAIM = f"resource_access.{API_CLIENT}.roles"
DEFAULT_REDIS_URL = "redis://redis:6379/0"
TENANT_A, TENANT_B = "tenant-alpha", "tenant-beta"
PLATFORM_TENANT = "rc-platform-ops"
RATE_LIMIT = 40
DESKTOP = {"width": 1440, "height": 900}
MOBILE = {"width": 390, "height": 844}
UI_TIMEOUT_MS = 20_000
JOB_TIMEOUT_MS = 45_000
PRIOR_PASS_GATES = (
    "docker_compose",
    "postgresql",
    "postgresql_concurrency",
    "redis_worker",
    "qdrant",
    "oidc",
    "external_llm_provider",
    "tenant_isolation",
    "model_router",
    "finops",
    "rate_limiting",
    "backup_restore",
    "readiness_failure_recovery",
)
QUESTION_A = "Welche Compliance-Richtlinien gelten für Kundendaten?"
QUESTION_B = "Welche Beta Atlas Verfuegbarkeit ist dokumentiert?"
ABSTENTION_TEXT = "Ich verweigere eine fachliche Antwort"
HOSTILE_QUESTION = (
    '<img src=x onerror="window.__ragopsXss=1"> <script>window.__ragopsXss=2</script>'
)
HOSTILE_TITLE = '<b>Quartal</b><img src=x onerror="window.__ragopsXss=3">'


class PrerequisiteMissing(RuntimeError):
    pass


@dataclass
class GateResult:
    checks: dict[str, bool] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    tenant_leakage: int = 0
    provider_invocations: int = 0
    paid_provider_calls: int = 0
    page_errors: list[str] = field(default_factory=list)
    console_errors: list[str] = field(default_factory=list)
    failed_requests: list[str] = field(default_factory=list)
    expected_failures: list[dict[str, str]] = field(default_factory=list)
    screenshots: list[str] = field(default_factory=list)
    traces: list[str] = field(default_factory=list)

    def check(self, name: str, ok: bool) -> bool:
        self.checks[name] = bool(ok) and self.checks.get(name, True)
        return bool(ok)


# --------------------------------------------------------------------------- utilities


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def http_json(
    url: str,
    method: str = "GET",
    data: object | None = None,
    *,
    token: str | None = None,
    form: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 10.0,
) -> tuple[int, Any, dict[str, str]]:
    """Small HTTP helper; never logs URLs, bodies or credentials."""
    body: bytes | None = None
    request_headers = {"Accept": "application/json", **(headers or {})}
    if form is not None:
        body = urllib.parse.urlencode(form).encode()
        request_headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif data is not None:
        body = json.dumps(data).encode()
        request_headers["Content-Type"] = "application/json"
    if token:
        request_headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=body, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            raw = response.read()
            return response.status, (json.loads(raw) if raw else None), dict(response.headers)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            payload = json.loads(raw) if raw else None
        except json.JSONDecodeError:
            payload = None
        return exc.code, payload, dict(exc.headers or {})


def wait_until(predicate: Callable[[], bool], timeout: float, interval: float = 0.25) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with suppress(Exception):
            if predicate():
                return True
        time.sleep(interval)
    return False


def synthetic_pdf(text: str) -> bytes:
    """A small, valid single-page PDF with synthetic text."""
    safe = text.replace("\\", "").replace("(", "").replace(")", "")
    stream = f"BT /F1 12 Tf 72 720 Td ({safe}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    ).encode()
    return bytes(out)


# --------------------------------------------------------------------------- Keycloak


def keycloak_admin_credentials() -> tuple[str, str] | None:
    """KEYCLOAK_ADMIN[_PASSWORD] from the environment, else from the local Keycloak container."""
    if os.getenv("KEYCLOAK_ADMIN_PASSWORD"):
        return os.getenv("KEYCLOAK_ADMIN", "admin"), str(os.getenv("KEYCLOAK_ADMIN_PASSWORD"))
    try:
        container = subprocess.run(
            [
                "docker",
                "ps",
                "-q",
                "--filter",
                "label=com.docker.compose.project=ragops-enterprise-copilot",
                "--filter",
                "label=com.docker.compose.service=keycloak",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        ).stdout.split()
        if len(container) != 1:
            return None
        raw = subprocess.run(
            ["docker", "inspect", "--format", "{{json .Config.Env}}", container[0]],
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    values = dict(item.split("=", 1) for item in json.loads(raw) if "=" in item)
    password = values.get("KEYCLOAK_ADMIN_PASSWORD")
    return (values.get("KEYCLOAK_ADMIN", "admin"), password) if password else None


class KeycloakAdmin:
    def __init__(self, username: str, password: str) -> None:
        self._username, self._password = username, password
        self._token = ""
        self._expires = 0.0

    def _admin_token(self) -> str:
        if time.monotonic() < self._expires:
            return self._token
        status, payload, _ = http_json(
            f"{KEYCLOAK_URL}/realms/master/protocol/openid-connect/token",
            "POST",
            form={
                "grant_type": "password",
                "client_id": "admin-cli",
                "username": self._username,
                "password": self._password,
            },
        )
        if status != 200 or not isinstance(payload, dict):
            raise PrerequisiteMissing("Keycloak admin login failed")
        self._token = str(payload["access_token"])
        self._expires = time.monotonic() + int(payload.get("expires_in", 60)) - 10
        return self._token

    def call(self, method: str, path: str, data: object | None = None) -> tuple[int, Any]:
        status, payload, _ = http_json(
            f"{KEYCLOAK_URL}/admin/realms/{REALM}{path}",
            method,
            data,
            token=self._admin_token(),
        )
        return status, payload

    def snapshot(self) -> dict[str, Any]:
        _, users = self.call("GET", "/users?max=1000&briefRepresentation=true")
        _, clients = self.call("GET", "/clients?max=1000")
        return {
            "users": sorted(str(user["username"]) for user in users or []),
            "clients": sorted(str(client["clientId"]) for client in clients or []),
        }

    def client_uuid(self, client_id: str) -> str | None:
        _, clients = self.call("GET", f"/clients?clientId={urllib.parse.quote(client_id)}")
        return str(clients[0]["id"]) if clients else None

    def user_uuid(self, username: str) -> str | None:
        _, users = self.call("GET", f"/users?exact=true&username={urllib.parse.quote(username)}")
        return str(users[0]["id"]) if users else None


@dataclass
class GateUser:
    label: str
    username: str
    password: str
    tenant: str
    role: str


def create_dashboard_client(
    admin: KeycloakAdmin, client_id: str, secret: str, redirect_uris: list[str]
) -> None:
    mappers = [
        {
            "name": "ragops-api-audience",
            "protocol": "openid-connect",
            "protocolMapper": "oidc-audience-mapper",
            "config": {
                "included.client.audience": API_CLIENT,
                "access.token.claim": "true",
                "id.token.claim": "false",
            },
        },
        {
            "name": "tenant-id",
            "protocol": "openid-connect",
            "protocolMapper": "oidc-usermodel-attribute-mapper",
            "config": {
                "user.attribute": "tenant_id",
                "claim.name": "tenant_id",
                "jsonType.label": "String",
                "access.token.claim": "true",
                "id.token.claim": "false",
                "userinfo.token.claim": "false",
            },
        },
    ]
    status, _ = admin.call(
        "POST",
        "/clients",
        {
            "clientId": client_id,
            "name": "RC browser E2E dashboard (temporary)",
            "enabled": True,
            "protocol": "openid-connect",
            "publicClient": False,
            "secret": secret,
            "standardFlowEnabled": True,
            # Only for the gate's direct API support checks; the login proof is the browser.
            "directAccessGrantsEnabled": True,
            "redirectUris": redirect_uris,
            "webOrigins": ["+"],
            "attributes": {"post.logout.redirect.uris": "##".join(redirect_uris)},
            "protocolMappers": mappers,
        },
    )
    if status != 201:
        raise PrerequisiteMissing("Keycloak dashboard client could not be created")


def create_user(admin: KeycloakAdmin, user: GateUser) -> None:
    status, _ = admin.call(
        "POST",
        "/users",
        {
            "username": user.username,
            "enabled": True,
            "firstName": "RC",
            "lastName": user.label,
            "email": f"{user.username}@rc-e2e.invalid",
            "emailVerified": True,
            "requiredActions": [],
            "attributes": {"tenant_id": [user.tenant]},
            "credentials": [{"type": "password", "value": user.password, "temporary": False}],
        },
    )
    if status != 201:
        raise PrerequisiteMissing("Keycloak gate user could not be created")
    user_id = admin.user_uuid(user.username)
    api_client = admin.client_uuid(API_CLIENT)
    if not user_id or not api_client:
        raise PrerequisiteMissing("Keycloak realm is missing the ragops-api client")
    _, role = admin.call("GET", f"/clients/{api_client}/roles/{user.role}")
    admin.call("POST", f"/users/{user_id}/role-mappings/clients/{api_client}", [role])


def realm_public_key() -> str:
    """PEM of the realm signing key, as the API verifies tokens against configured keys."""
    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicNumbers
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    _, discovery, _ = http_json(f"{KEYCLOAK_URL}/realms/{REALM}/.well-known/openid-configuration")
    _, jwks, _ = http_json(str(discovery["jwks_uri"]))
    key = next(
        item
        for item in jwks["keys"]
        if item.get("kty") == "RSA"
        and item.get("use", "sig") == "sig"
        and item.get("alg") == "RS256"
    )

    def number(value: str) -> int:
        import base64

        return int.from_bytes(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)), "big")

    return (
        RSAPublicNumbers(number(key["e"]), number(key["n"]))
        .public_key()
        .public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
        .decode()
    )


def password_token(client_id: str, secret: str, user: GateUser) -> str:
    """Support-check token for direct API assertions; never the gate's login proof."""
    status, payload, _ = http_json(
        f"{KEYCLOAK_URL}/realms/{REALM}/protocol/openid-connect/token",
        "POST",
        form={
            "grant_type": "password",
            "client_id": client_id,
            "client_secret": secret,
            "username": user.username,
            "password": user.password,
            "scope": "openid",
        },
    )
    if status != 200 or not isinstance(payload, dict):
        raise RuntimeError("support token request failed")
    return str(payload["access_token"])


# --------------------------------------------------------------------------- processes


class Service:
    def __init__(self, name: str, command: list[str], env: dict[str, str], log: Path) -> None:
        self.name, self.log = name, log
        self._handle = log.open("wb")
        self.process = subprocess.Popen(
            command, cwd=ROOT, env=env, stdout=self._handle, stderr=subprocess.STDOUT
        )

    def alive(self) -> bool:
        return self.process.poll() is None

    def stop(self) -> bool:
        if self.alive():
            self.process.terminate()
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self._handle.close()
        return not self.alive()


def base_env() -> dict[str, str]:
    """Inherited environment minus every product, provider and identity setting."""
    blocked = ("RAGOPS_", "OPENAI_", "AZURE_", "KEYCLOAK_", "STREAMLIT_", "ANTHROPIC_")
    env = {key: value for key, value in os.environ.items() if not key.startswith(blocked)}
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT), str(ROOT / "src")])
    env["PYTHONUNBUFFERED"] = "1"
    return env


def http_status(url: str, timeout: float = 2.0) -> int:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)


def wait_http(url: str, timeout: float, expect: int = 200) -> bool:
    return wait_until(lambda: http_status(url) == expect, timeout, 0.3)


def dashboard_secrets(path: Path, *, port: int, client_id: str, secret: str, metadata: str) -> None:
    lines = [
        "[auth]",
        f'redirect_uri = "http://localhost:{port}/oauth2callback"',
        f'cookie_secret = "{secrets.token_urlsafe(32)}"',
        f'client_id = "{client_id}"',
        f'client_secret = "{secret}"',
        f'server_metadata_url = "{metadata}"',
        'expose_tokens = ["access"]',
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o600)


def start_dashboard(
    name: str, port: int, secrets_file: Path, env: dict[str, str], logs: Path
) -> Service:
    command = [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(ROOT / "apps" / "dashboard" / "dashboard.py"),
        "--server.address=127.0.0.1",
        f"--server.port={port}",
        "--server.headless=true",
        "--server.fileWatcherType=none",
        "--browser.gatherUsageStats=false",
        "--server.maxUploadSize=2",
        "--client.toolbarMode=viewer",
        "--client.showErrorDetails=none",
        f"--secrets.files={secrets_file}",
    ]
    return Service(name, command, env, logs / f"{name}.log")


# --------------------------------------------------------------------------- environment


@dataclass
class Environment:
    run_id: str
    ui_url: str
    ui_idp_down_url: str
    api_url: str
    client_id: str
    client_secret: str
    users: dict[str, GateUser]
    database_url: str
    redis_direct: str
    queue_proxy: FaultProxy
    queue_name: str
    redis_prefix: str
    workspaces: dict[str, tuple[str, str, str, str]]  # tenant -> (ws id, ws name, coll id, name)
    admin: KeycloakAdmin
    ledger: Path
    work: Path
    # For gates that add their own processes; everything listed here is stopped on cleanup.
    service_env: dict[str, str] = field(default_factory=dict)
    services: list[Service] = field(default_factory=list)

    def token(self, label: str) -> str:
        return password_token(self.client_id, self.client_secret, self.users[label])

    def api(self, path: str, label: str | None = None, **kwargs: Any) -> tuple[int, Any]:
        token = self.token(label) if label else None
        status, payload, _ = http_json(f"{self.api_url}{path}", token=token, **kwargs)
        return status, payload


def _unrelated_postgres_digest(base_url: str) -> str:
    from sqlalchemy import create_engine

    from ragops.ops import data_backup

    engine = create_engine(base_url, hide_parameters=True)
    try:
        with engine.connect() as connection:
            return hashlib.sha256(
                json.dumps(data_backup.export_postgres(connection), sort_keys=True).encode()
            ).hexdigest()
    finally:
        engine.dispose()


def _database_names(base_url: str) -> list[str]:
    from sqlalchemy import create_engine, text

    engine = create_engine(base_url, hide_parameters=True, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as connection:
            return sorted(
                str(name)
                for name in connection.execute(
                    text("SELECT datname FROM pg_database WHERE NOT datistemplate")
                ).scalars()
            )
    finally:
        engine.dispose()


@contextmanager
def gate_database(base_url: str, run_id: str) -> Iterator[str]:
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    name = f"ragops_test_e2e_{run_id}"
    if not GATE_DATABASE.match(name):
        raise ValueError("refusing a database name outside the gate prefix")
    admin = create_engine(base_url, hide_parameters=True, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{name}"'))
        yield make_url(base_url).set(database=name).render_as_string(hide_password=False)
    finally:
        with admin.connect() as connection:
            connection.execute(
                text(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = :name AND pid <> pg_backend_pid()"
                ),
                {"name": name},
            )
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}"'))
        admin.dispose()


def _seed_knowledge(database_url: str, work: Path, run_id: str) -> dict[str, tuple[str, ...]]:
    from sqlalchemy import create_engine

    from ragops.knowledge.service import KnowledgeManagementService, LocalDocumentBlobStore
    from ragops.persistence.database import transaction
    from ragops.persistence.models import Tenant

    engine = create_engine(database_url, hide_parameters=True)
    seeded: dict[str, tuple[str, ...]] = {}
    try:
        for tenant, label in ((TENANT_A, "Alpha"), (TENANT_B, "Beta")):
            with transaction(engine) as session:
                session.add(Tenant(id=tenant, name=f"RC E2E {label}"))
                session.flush()
                service = KnowledgeManagementService(
                    session, tenant, "rc-e2e-seed", LocalDocumentBlobStore(work / "blobs")
                )
                workspace_name = f"E2E {label} Workspace {run_id[:4]}"
                collection_name = f"E2E {label} Collection"
                workspace = service.create_workspace(workspace_name)
                collection = service.create_collection(workspace.id, collection_name)
                seeded[tenant] = (
                    str(workspace.id),
                    workspace_name,
                    str(collection.id),
                    collection_name,
                )
    finally:
        engine.dispose()
    return seeded


def _redis_keys(url: str, pattern: str) -> set[str]:
    import redis

    client = redis.Redis.from_url(url, socket_timeout=3, decode_responses=True)
    try:
        return set(client.scan_iter(match=pattern, count=500))
    finally:
        client.close()


@contextmanager
def environment(result: GateResult, evidence: dict[str, Any]) -> Iterator[Environment]:
    import redis

    from scripts.backup_restore_gate import resolve_test_database_url
    from scripts.postgres_concurrency_gate import (
        migration_revision,
        run_alembic,
        validate_isolated_database,
    )
    from scripts.tenant_isolation_gate import _disposable_qdrant, _redis_endpoint

    run_id = str(evidence["run_id"])
    credentials = keycloak_admin_credentials()
    if credentials is None:
        raise PrerequisiteMissing("local Keycloak admin credentials are unavailable")
    if not wait_http(f"{KEYCLOAK_URL}/realms/{REALM}/.well-known/openid-configuration", 10):
        raise PrerequisiteMissing("local Keycloak realm is unavailable")
    base_url = resolve_test_database_url()
    if not base_url or not validate_isolated_database(base_url)[0]:
        raise PrerequisiteMissing("isolated PostgreSQL credentials are unavailable")
    redis_direct = _redis_endpoint(os.getenv("RAGOPS_REDIS_URL") or DEFAULT_REDIS_URL, evidence)
    if redis_direct is None:
        raise PrerequisiteMissing("RC Redis is unavailable")
    admin = KeycloakAdmin(*credentials)
    keycloak_before = admin.snapshot()
    databases_before = _database_names(base_url)
    postgres_before = _unrelated_postgres_digest(base_url)
    redis_prefix = f"ragops:rc:e2e:{run_id}"
    redis_before = _redis_keys(redis_direct, "*") - _redis_keys(redis_direct, f"{redis_prefix}*")
    sentinel_key = f"{redis_prefix}:sentinel"
    sentinel_client = redis.Redis.from_url(redis_direct, socket_timeout=3, decode_responses=True)
    sentinel_value = uuid4().hex
    corpus_digest = _tree_digest(ROOT / "data" / "synthetic")
    tracked_before = _git_status()
    client_id = f"rc-e2e-dashboard-{run_id}"
    client_secret = secrets.token_urlsafe(32)
    users = {
        label: GateUser(
            label,
            f"rc-e2e-{run_id}-{label}",
            secrets.token_urlsafe(18) + "Aa1!",
            tenant,
            role,
        )
        for label, tenant, role in (
            ("a-admin", TENANT_A, "admin"),
            ("a-viewer", TENANT_A, "viewer"),
            ("a-support", TENANT_A, "support"),
            ("b-admin", TENANT_B, "admin"),
        )
    }
    ui_port, idp_down_port, api_port = free_port(), free_port(), free_port()
    services: list[Service] = []
    stack = ExitStack()
    work = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="rc-e2e-")))
    queue_proxy: FaultProxy | None = None
    try:
        sentinel_client.set(sentinel_key, sentinel_value, ex=1800)
        qdrant = stack.enter_context(_disposable_qdrant(evidence))
        if qdrant is None:
            raise PrerequisiteMissing("RC Qdrant service is unavailable")
        database_url = stack.enter_context(gate_database(base_url, run_id))
        run_alembic(database_url, "upgrade", "head")
        result.check(
            "migration_at_head",
            migration_revision(run_alembic(database_url, "current").stdout) is not None,
        )
        from ragops.vector.index import QdrantVectorIndex

        collection = f"rc_e2e_{run_id}"
        QdrantVectorIndex(qdrant, collection, 64, timeout_seconds=5).ensure_schema()
        shutil.copytree(ROOT / "data" / "synthetic", work / "data")
        (work / "evidence").mkdir()
        (work / "logs").mkdir()
        seeded = _seed_knowledge(database_url, work / "data", run_id)
        redirects = [
            f"http://localhost:{ui_port}/oauth2callback",
            f"http://localhost:{idp_down_port}/oauth2callback",
        ]
        create_dashboard_client(admin, client_id, client_secret, redirects)
        for user in users.values():
            create_user(admin, user)
        redis_host, redis_port = _redis_host_port(redis_direct)
        queue_proxy = FaultProxy(redis_host, redis_port)
        redis_db = urllib.parse.urlsplit(redis_direct).path or "/0"
        queue_url = f"redis://127.0.0.1:{queue_proxy.port}{redis_db}"
        ledger = work / "provider-ledger.txt"
        ledger.touch()
        issuer = f"{KEYCLOAK_URL}/realms/{REALM}"
        oidc = {
            "RAGOPS_IDENTITY_PROVIDER": "oidc",
            "RAGOPS_OIDC_ISSUER": issuer,
            "RAGOPS_OIDC_AUDIENCE": API_CLIENT,
            "RAGOPS_OIDC_PUBLIC_KEY": realm_public_key(),
            "RAGOPS_OIDC_ROLES_CLAIM": ROLES_CLAIM,
            "RAGOPS_OIDC_TENANT_CLAIM": "tenant_id",
            "RAGOPS_OIDC_ALLOWED_CLIENTS": client_id,
        }
        product = {
            **base_env(),
            **oidc,
            "RAGOPS_ENV": "test",
            "RAGOPS_DATABASE_URL": database_url,
            "RAGOPS_DATA_DIR": str(work / "data"),
            "RAGOPS_EVIDENCE_DIR": str(work / "evidence"),
            "RAGOPS_REDIS_URL": queue_url,
            "RAGOPS_QUEUE_NAME": f"{redis_prefix}:ingestion",
            "RAGOPS_ASYNC_INGESTION_REQUIRED": "true",
            "RAGOPS_RATE_LIMIT_BACKEND": "redis",
            "RAGOPS_RATE_LIMIT_REDIS_URL": redis_direct,
            "RAGOPS_RATE_LIMIT_NAMESPACE": f"{redis_prefix}:rate-limit",
            "RAGOPS_RATE_LIMIT_REQUESTS": str(RATE_LIMIT),
            "RAGOPS_RATE_LIMIT_WINDOW_SECONDS": "60",
            "RAGOPS_VECTOR_PROVIDER": "qdrant",
            # The browser journeys ask questions of the synthetic demo corpus; uploads still
            # go through the worker into Qdrant. The vector query path has its own gate.
            "RAGOPS_QUERY_MODE": "demo",
            "RAGOPS_QDRANT_URL": qdrant,
            "RAGOPS_QDRANT_COLLECTION": collection,
            "RAGOPS_LLM_PROVIDER": "deterministic",
            "RAGOPS_DEPENDENCY_TIMEOUT_SECONDS": "1",
            "RAGOPS_WORKER_BACKOFF_MAX_SECONDS": "1",
            "RAGOPS_E2E_PROVIDER_LEDGER": str(ledger),
            "RAGOPS_PLATFORM_ADMIN_TENANT_ID": PLATFORM_TENANT,
        }
        launcher = str(ROOT / "scripts" / "browser_e2e_services.py")
        logs = work / "logs"
        services.append(
            Service(
                "api", [sys.executable, launcher, "api", str(api_port)], product, logs / "api.log"
            )
        )
        services.append(
            Service("worker", [sys.executable, launcher, "worker"], product, logs / "worker.log")
        )
        api_url = f"http://127.0.0.1:{api_port}"
        if not wait_http(f"{api_url}/health", 30) or not wait_http(f"{api_url}/ready", 30):
            raise RuntimeError("gate API did not become ready")
        dashboard_env = {**base_env(), **oidc, "RAGOPS_ENV": "test", "RAGOPS_API_URL": api_url}
        metadata = f"{issuer}/.well-known/openid-configuration"
        secrets_file = work / "dashboard-secrets.toml"
        dashboard_secrets(
            secrets_file, port=ui_port, client_id=client_id, secret=client_secret, metadata=metadata
        )
        # A second dashboard whose identity provider is unreachable proves the sign-in
        # outage state without touching the shared Keycloak.
        down_secrets = work / "dashboard-idp-down.toml"
        dashboard_secrets(
            down_secrets,
            port=idp_down_port,
            client_id=client_id,
            secret=client_secret,
            metadata=f"http://127.0.0.1:{free_port()}/realms/{REALM}/.well-known/openid-configuration",
        )
        services.append(start_dashboard("dashboard", ui_port, secrets_file, dashboard_env, logs))
        services.append(
            start_dashboard("dashboard-idp-down", idp_down_port, down_secrets, dashboard_env, logs)
        )
        for port in (ui_port, idp_down_port):
            if not wait_http(f"http://127.0.0.1:{port}/_stcore/health", 60):
                raise RuntimeError("gate dashboard did not start")
        evidence["ui_start_command_sanitized"] = (
            "python -m streamlit run apps/dashboard/dashboard.py --server.address=127.0.0.1 "
            "--server.port=<gate-port> --server.headless=true --secrets.files=<gate-temp-file>"
        )
        evidence["api_start_command_sanitized"] = (
            "python scripts/browser_e2e_services.py api <gate-port> (uvicorn apps.api.main:app)"
        )
        yield Environment(
            run_id=run_id,
            ui_url=f"http://localhost:{ui_port}",
            ui_idp_down_url=f"http://localhost:{idp_down_port}",
            api_url=api_url,
            client_id=client_id,
            client_secret=client_secret,
            users=users,
            database_url=database_url,
            redis_direct=redis_direct,
            queue_proxy=queue_proxy,
            queue_name=f"{redis_prefix}:ingestion",
            redis_prefix=redis_prefix,
            workspaces={tenant: values for tenant, values in seeded.items()},  # type: ignore[misc]
            admin=admin,
            ledger=ledger,
            work=work,
            service_env=product,
            services=services,
        )
    finally:
        stopped = [service.stop() for service in services]
        result.check("processes_stopped", all(stopped))
        if queue_proxy is not None:
            queue_proxy.stop()
        try:
            ledger_lines = (work / "provider-ledger.txt").read_text(encoding="utf-8").split()
        except OSError:
            ledger_lines = []
        result.provider_invocations = ledger_lines.count("invoke")
        result.paid_provider_calls = ledger_lines.count("paid")
        for user in users.values():
            user_id = admin.user_uuid(user.username)
            if user_id:
                admin.call("DELETE", f"/users/{user_id}")
        client_uuid = admin.client_uuid(client_id)
        if client_uuid:
            admin.call("DELETE", f"/clients/{client_uuid}")
        result.check("keycloak_gate_objects_removed", admin.snapshot() == keycloak_before)
        stored = sentinel_client.get(sentinel_key)
        result.check("unrelated_redis_sentinel_unchanged", stored == sentinel_value)
        gate_keys = _redis_keys(redis_direct, f"{redis_prefix}*")
        if gate_keys:
            sentinel_client.delete(*gate_keys)
        sentinel_client.close()
        result.check("redis_gate_keys_removed", not _redis_keys(redis_direct, f"{redis_prefix}*"))
        outside_after = _redis_keys(redis_direct, "*") - _redis_keys(
            redis_direct, f"{redis_prefix}*"
        )
        # Keys of earlier gates may expire meanwhile; the gate must not create new ones.
        result.check("unrelated_redis_data_unchanged", outside_after <= redis_before)
        stack.close()
        result.check("gate_database_removed", _database_names(base_url) == databases_before)
        result.check(
            "unrelated_postgres_data_unchanged",
            _unrelated_postgres_digest(base_url) == postgres_before,
        )
        result.check(
            "unrelated_files_unchanged",
            _tree_digest(ROOT / "data" / "synthetic") == corpus_digest
            and _git_status() == tracked_before,
        )
        result.check(
            "disposable_qdrant_removed", evidence.get("qdrant_service_cleanup_status") != "FAIL"
        )


def _redis_host_port(url: str) -> tuple[str, int]:
    parts = urllib.parse.urlsplit(url)
    return parts.hostname or "127.0.0.1", parts.port or 6379


def _tree_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digest.update(str(path.relative_to(root)).encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def _git_status() -> str:
    return subprocess.run(
        ["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True, check=False
    ).stdout


# --------------------------------------------------------------------------- browser


PAGE_TITLES = {
    "Copilot": "Enterprise Copilot",
    "Wissensbasis": "Wissensbasis",
    "Monitoring": "Monitoring",
    "FinOps": "AI FinOps",
    "Governance & Audit": "Governance & Audit",
    "System / Operations": "System / Operations",
}
PERMISSION_TEXT = "fehlt Ihrer Rolle die Berechtigung"
OUTAGE_TEXT = "vorübergehend nicht verfügbar"
SECRET_PATTERNS = re.compile(
    r"eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}|Bearer\s+[A-Za-z0-9._-]{20,}"
    r"|postgresql\+psycopg://|redis://[^\s\"']*@|Traceback \(most recent call last\)"
)


def _first_line(exc: BaseException) -> str:
    """First line of an error plus the locator Playwright was waiting for, sanitized."""
    lines = [line.strip() for line in str(exc).splitlines() if line.strip()]
    waiting = next((line for line in lines if line.startswith("- waiting for")), "")
    return _sanitize(" ".join(filter(None, [lines[0] if lines else "", waiting])))


def _sanitize(text: str) -> str:
    text = re.sub(r"\?[^\s\"']*", "?<query>", text)
    return SECRET_PATTERNS.sub("<redacted>", text)[:200]


class BrowserSession:
    """One Chromium with per-context listeners that classify every runtime failure."""

    def __init__(self, playwright: Any, result: GateResult, run_dir: Path) -> None:
        self.browser = playwright.chromium.launch(channel="chromium", headless=True)
        self.result, self.run_dir = result, run_dir
        self.allowances: list[tuple[re.Pattern[str], str]] = []
        self.version = str(self.browser.version)

    def allow(self, pattern: str, reason: str) -> None:
        self.allowances.append((re.compile(pattern), reason))

    def _classify(self, kind: str, text: str, bucket: list[str]) -> None:
        clean = _sanitize(text)
        for pattern, reason in self.allowances:
            if pattern.search(clean):
                self.result.expected_failures.append(
                    {"kind": kind, "event": clean, "reason": reason}
                )
                return
        bucket.append(clean)

    def context(self, viewport: dict[str, int]) -> tuple[Any, Any]:
        context = self.browser.new_context(viewport=viewport, locale="de-DE")
        page = context.new_page()
        page.set_default_timeout(UI_TIMEOUT_MS)
        result = self.result
        page.on(
            "pageerror", lambda error: self._classify("pageerror", str(error), result.page_errors)
        )
        page.on(
            "console",
            lambda message: self._classify(
                "console",
                f"{message.text} @ {message.location.get('url', '')}",
                result.console_errors,
            )
            if message.type == "error"
            else None,
        )
        page.on(
            "requestfailed",
            lambda request: self._classify(
                "requestfailed",
                f"{request.method} {request.url} {request.failure}",
                result.failed_requests,
            ),
        )
        page.on(
            "response",
            lambda response: self._classify(
                "response",
                f"{response.request.method} {response.url} {response.status}",
                result.failed_requests,
            )
            if response.status >= 400
            else None,
        )
        return context, page

    def screenshot(self, page: Any, name: str) -> None:
        path = self.run_dir / f"{len(self.result.screenshots) + 1:02d}-{name}.png"
        page.screenshot(path=str(path), full_page=False)
        self.result.screenshots.append(path.name)

    def close(self) -> None:
        self.browser.close()


def main_area(page: Any) -> Any:
    # Present in both layouts; pages with a chat input use a different outer section.
    return page.get_by_test_id("stMainBlockContainer")


def open_sidebar(page: Any) -> Any:
    sidebar = page.get_by_test_id("stSidebar")
    radios = sidebar.locator("[role=radiogroup]")
    expand = page.get_by_test_id("stExpandSidebarButton")
    radios.or_(expand).first.wait_for()

    def on_screen() -> bool:
        box = radios.bounding_box() if radios.count() else None
        return bool(box and box["x"] >= 0 and box["width"] > 20)

    if not on_screen():
        expand.click()
        if not wait_until(on_screen, UI_TIMEOUT_MS / 1000, 0.2):
            raise TimeoutError("sidebar did not open")
    return sidebar


def navigate(page: Any, item: str) -> None:
    open_sidebar(page).get_by_text(item, exact=True).click()
    main_area(page).get_by_role("heading", name=PAGE_TITLES[item]).wait_for()


def keycloak_login(page: Any, user: GateUser, *, password: str | None = None) -> None:
    page.wait_for_url(f"{KEYCLOAK_URL}/**")
    page.locator("#username").fill(user.username)
    page.locator("#password").fill(password or user.password)
    page.locator("#kc-login").click()


def login(session: BrowserSession, page: Any, env: Environment, label: str) -> None:
    page.goto(env.ui_url)
    main_area(page).get_by_role("heading", name="Anmeldung erforderlich").wait_for()
    page.get_by_role("button", name="Anmelden").click()
    keycloak_login(page, env.users[label])
    page.wait_for_url(f"{env.ui_url}/**")
    page.get_by_text("Angemeldet als", exact=False).first.wait_for()


def no_horizontal_overflow(page: Any) -> bool:
    return bool(
        page.evaluate("() => document.documentElement.scrollWidth <= window.innerWidth + 1")
    )


def unobstructed(page: Any, locator: Any) -> bool:
    """The control is visible and the topmost element at its centre belongs to it."""
    box = locator.bounding_box()
    if not box or box["width"] <= 0 or box["height"] <= 0:
        return False
    return bool(
        locator.evaluate(
            "(el, p) => { const top = document.elementFromPoint(p.x, p.y);"
            " return !!top && (el === top || el.contains(top) || top.contains(el)); }",
            {"x": box["x"] + box["width"] / 2, "y": box["y"] + box["height"] / 2},
        )
    )


def xss_absent(page: Any) -> bool:
    return bool(
        page.evaluate(
            "() => window.__ragopsXss === undefined && !document.querySelector('img[src=\"x\"]')"
        )
    )


def ask(page: Any, question: str) -> None:
    box = page.get_by_test_id("stChatInputTextArea")
    box.fill(question)
    box.press("Enter")


def job_row(page: Any, text: str) -> Any:
    return page.get_by_test_id("stTable").locator("tr", has_text=text)


def upload_via_ui(
    page: Any,
    *,
    title: str,
    name: str,
    content: bytes,
    mime: str,
    double: bool = False,
    expect_listed: bool = True,
) -> None:
    main = main_area(page)
    main.get_by_label("Titel").fill(title)
    main.locator("input[type=file]").set_input_files(
        {"name": name, "mimeType": mime, "buffer": content}
    )
    if expect_listed:
        main.get_by_test_id("stFileChip").first.wait_for()
    button = main.get_by_role("button", name="Hochladen")
    if double:
        button.dblclick()
    else:
        button.click()


def observe_job(page: Any, title: str, until: str, timeout_ms: int = JOB_TIMEOUT_MS) -> list[str]:
    """Record every status label the job row shows until it reaches ``until``."""
    seen: list[str] = []
    deadline = time.monotonic() + timeout_ms / 1000
    labels = {label: raw for raw, label in STATUS_LABELS.items()}
    while time.monotonic() < deadline:
        with suppress(Exception):
            row = job_row(page, title)
            if row.count():
                text = row.first.inner_text(timeout=2000)
                for label, raw in labels.items():
                    if label in text and (not seen or seen[-1] != raw):
                        seen.append(raw)
                if STATUS_LABELS[until] in text:
                    return seen
        page.wait_for_timeout(250)
    return seen


STATUS_LABELS = {
    "queued": "In Warteschlange",
    "running": "In Verarbeitung",
    "retrying": "Wird wiederholt",
    "completed": "Bereit",
    "failed": "Fehlgeschlagen",
    "cancelled": "Abgebrochen",
}


def choose_option(page: Any, label: str, option: str) -> None:
    """Select a Streamlit selectbox option by typing it, then verify the selection."""
    box = main_area(page).get_by_test_id("stSelectbox").filter(has_text=label)
    box.locator("input").click()
    page.keyboard.type(option)
    page.keyboard.press("Enter")
    if not wait_until(lambda: option in box.inner_text() or option in page.content(), 5):
        raise TimeoutError(f"option not selected in {label}")


def db_scalar(env: Environment, sql: str, **params: Any) -> Any:
    from sqlalchemy import create_engine, text

    engine = create_engine(env.database_url, hide_parameters=True)
    try:
        with engine.connect() as connection:
            return connection.execute(text(sql), params).scalar()
    finally:
        engine.dispose()


def versions_with_hash(env: Environment, content: bytes) -> int:
    return int(
        db_scalar(
            env,
            "SELECT count(*) FROM document_versions WHERE content_hash = :hash",
            hash=hashlib.sha256(content).hexdigest(),
        )
    )


def jobs_for_hash(env: Environment, content: bytes) -> int:
    return int(
        db_scalar(
            env,
            "SELECT count(*) FROM ingestion_jobs j JOIN document_versions v "
            "ON v.tenant_id = j.tenant_id AND v.id = j.document_version_id "
            "WHERE v.content_hash = :hash",
            hash=hashlib.sha256(content).hexdigest(),
        )
    )


def direct_upload(
    env: Environment, label: str, tenant: str, title: str, name: str, content: bytes
) -> dict[str, Any]:
    workspace_id, _, collection_id, _ = env.workspaces[tenant]
    boundary = uuid4().hex
    fields = {
        "workspace_id": workspace_id,
        "collection_id": collection_id,
        "title": title,
        "logical_document_key": re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-"),
    }
    body = b"".join(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
        for key, value in fields.items()
    )
    body += (
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\n'
            "Content-Type: application/pdf\r\n\r\n"
        ).encode()
        + content
        + f"\r\n--{boundary}--\r\n".encode()
    )
    request = urllib.request.Request(
        f"{env.api_url}/v1/documents/upload",
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Authorization": f"Bearer {env.token(label)}",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310
        return dict(json.loads(response.read()))


def axe_violations(page: Any) -> list[dict[str, Any]]:
    from axe_playwright_python.sync_playwright import Axe  # type: ignore[import-untyped]

    results = Axe().run(page)
    return [
        {
            "id": item.get("id"),
            "impact": item.get("impact"),
            "nodes": len(item.get("nodes", [])),
            "targets": [_sanitize(str(node.get("target"))) for node in item.get("nodes", [])[:3]],
        }
        for item in results.response.get("violations", [])
    ]


def focus_indicator(page: Any) -> dict[str, Any]:
    """Describe the focused element and whether it (or its label wrapper) shows focus."""
    return dict(
        page.evaluate(
            """() => {
              const el = document.activeElement;
              if (!el || el === document.body) return {tag: 'body', visible: false, name: ''};
              const shows = (node) => {
                if (!node) return false;
                const s = getComputedStyle(node);
                return (s.outlineStyle !== 'none' && parseFloat(s.outlineWidth) > 0)
                  || (s.boxShadow && s.boxShadow !== 'none');
              };
              const chain = [];
              for (let node = el; node && chain.length < 6; node = node.parentElement) {
                chain.push(node);
              }
              return {
                tag: el.tagName.toLowerCase(),
                testid: (el.closest('[data-testid]') || {}).dataset?.testid || '',
                name: (el.getAttribute('aria-label') || el.innerText || el.value || '')
                  .slice(0, 40),
                visible: chain.some(shows),
              };
            }"""
        )
    )


# --------------------------------------------------------------------------- journeys


class Journey:
    """Runs one journey; a failure is recorded, traced and never stops later journeys."""

    def __init__(self, session: BrowserSession, name: str, page: Any = None) -> None:
        self.session, self.name, self.page = session, name, page
        self._context: Any = None

    def trace(self, context: Any) -> None:
        # Tracing starts only after the password was submitted, so no trace can hold it.
        context.tracing.start(screenshots=True, snapshots=True, sources=False)
        self._context = context

    def __enter__(self) -> Journey:
        return self

    def __exit__(self, exc_type: Any, exc: BaseException | None, _tb: Any) -> bool:
        result = self.session.result
        result.check(f"journey_{self.name}_completed", exc is None)
        if exc is not None:
            result.errors.append(f"{self.name}: {type(exc).__name__}: {_first_line(exc)}")
            if self.page is not None:
                with suppress(Exception):
                    self.session.screenshot(self.page, f"failed-{self.name}")
        if self._context is not None:
            if exc is not None:
                path = self.session.run_dir / f"trace-{self.name}.zip"
                with suppress(Exception):
                    self._context.tracing.stop(path=str(path))
                    result.traces.append(path.name)
            else:
                with suppress(Exception):
                    self._context.tracing.stop()
        return True  # recorded; continue with the next journey


def source_titles(page: Any) -> list[str]:
    main = main_area(page)
    expanders = main.get_by_text(re.compile(r"^Quellen · \d+$"))
    expanders.last.wait_for()
    expanders.last.click()
    titles = main.locator(".source-title")
    titles.first.wait_for()
    return [title.strip() for title in titles.all_inner_texts()]


def tenant_titles(env: Environment, label: str) -> set[str]:
    status, documents = env.api("/v1/documents", label)
    return {str(item["title"]) for item in documents or []} if status == 200 else set()


def run_journeys(session: BrowserSession, env: Environment, evidence: dict[str, Any]) -> None:  # noqa: C901 - one linear script of observable browser journeys
    result = session.result
    run = env.run_id
    ws_a, ws_name_a, _, coll_name_a = env.workspaces[TENANT_A]
    ws_b, ws_name_b, _, coll_name_b = env.workspaces[TENANT_B]
    titles_a, titles_b = tenant_titles(env, "a-admin"), tenant_titles(env, "b-admin")
    result.check("support_tenant_corpora_disjoint", bool(titles_a) and not titles_a & titles_b)
    responsive_desktop: list[bool] = []
    responsive_mobile: list[bool] = []
    axe: list[dict[str, Any]] = []
    states_seen: set[str] = set()
    a_markers = {ws_name_a, coll_name_a, f"bericht-{run}.pdf", f"Doppelklick {run}"}
    b_title = f"Beta Vertraulich {run}"
    b_markers = {ws_name_b, coll_name_b, b_title, f"beta-{run}.pdf"}

    # Tenant B owns one processed document so isolation is tested against real data.
    b_accept = direct_upload(
        env, "b-admin", TENANT_B, b_title, f"beta-{run}.pdf", synthetic_pdf(f"Beta {run}")
    )
    b_job = str(b_accept["job_id"])
    b_markers.add(b_job[:8])

    ctx_a, page = session.context(DESKTOP)
    with Journey(session, "login", page) as journey:
        page.goto(env.ui_url)
        main_area(page).get_by_role("heading", name="Anmeldung erforderlich").wait_for()
        result.check(
            "unauthenticated_shows_only_login",
            page.get_by_text("Angemeldet als").count() == 0
            and main_area(page).get_by_role("heading", name="Enterprise Copilot").count() == 0,
        )
        axe.append({"page": "login", "violations": axe_violations(page)})
        session.screenshot(page, "login-required")
        page.get_by_role("button", name="Anmelden").click()
        page.wait_for_url(f"{KEYCLOAK_URL}/**")
        result.check(
            "keycloak_redirect_verified",
            page.url.startswith(f"{KEYCLOAK_URL}/realms/{REALM}/protocol/openid-connect/auth")
            and page.locator("#username").is_visible()
            and page.locator("#password").is_visible(),
        )
        session.screenshot(page, "keycloak-login-form-empty")
        keycloak_login(page, env.users["a-admin"])
        page.wait_for_url(f"{env.ui_url}/**")
        page.get_by_text("Angemeldet als").first.wait_for()
        journey.trace(ctx_a)
        result.check("real_browser_login_verified", True)
        result.check("return_path_preserved", urllib.parse.urlsplit(page.url).path in {"", "/"})
        sidebar_text = open_sidebar(page).inner_text()
        result.check(
            "tenant_role_context_loaded",
            "Atlas Industrial" in sidebar_text and "Administration" in sidebar_text,
        )
        result.check(
            "session_cookie_http_only",
            any(
                cookie["name"].startswith("_streamlit_user") and cookie["httpOnly"]
                for cookie in ctx_a.cookies()
            ),
        )
        page.reload()
        page.get_by_text("Angemeldet als").first.wait_for()
        result.check("direct_reload_keeps_session", True)

    with Journey(session, "navigation_admin", page):
        admin_markers = {
            "Monitoring": "Anfragen",
            "FinOps": "Spend",
            "Governance & Audit": "AI Providers und Model Routing",
            "System / Operations": "Readiness: ready",
        }
        ok = True
        for item in PAGE_TITLES:
            navigate(page, item)
            if item in admin_markers:
                main_area(page).get_by_text(admin_markers[item], exact=False).first.wait_for()
            text = main_area(page).inner_text()
            ok = ok and PERMISSION_TEXT not in text and OUTAGE_TEXT not in text
            responsive_desktop.append(no_horizontal_overflow(page))
        result.check("navigation_all_pages", True)
        result.check("admin_pages_succeed", ok)

    with Journey(session, "query", page):
        navigate(page, "Copilot")
        ask(page, QUESTION_A)
        shown = source_titles(page)
        status, answer = env.api(
            "/v1/query", "a-admin", method="POST", data={"question": QUESTION_A, "top_k": 5}
        )
        api_titles = {str(item["title"]) for item in (answer or {}).get("citations", [])}
        result.check("query_answer_rendered", status == 200 and bool(shown))
        result.check(
            "citations_or_sources_verified",
            bool(shown) and set(shown) <= titles_a and set(shown) <= api_titles | titles_a,
        )
        result.tenant_leakage += len(set(shown) & (titles_b - titles_a))
        responsive_desktop.append(
            no_horizontal_overflow(page)
            and unobstructed(page, page.get_by_test_id("stChatInputTextArea"))
        )
        session.screenshot(page, "copilot-answer-sources")
        axe.append({"page": "copilot", "violations": axe_violations(page)})
        ask(page, HOSTILE_QUESTION)
        main_area(page).get_by_text("<img src=x", exact=False).first.wait_for()
        page.wait_for_timeout(500)
        result.check("hostile_query_rendered_literally", xss_absent(page))

    pdf_report = synthetic_pdf(f"RC E2E Bericht {run}")
    with Journey(session, "upload", page):
        navigate(page, "Wissensbasis")
        axe.append({"page": "knowledge", "violations": axe_violations(page)})
        main_area(page).get_by_role("button", name="Hochladen").wait_for()
        result.check(
            "upload_ui_present",
            main_area(page).locator("input[type=file]").count() == 1
            and main_area(page).get_by_role("button", name="Hochladen").is_visible(),
        )
        responsive_desktop.append(
            unobstructed(page, main_area(page).get_by_role("button", name="Hochladen"))
        )
        upload_via_ui(
            page,
            title=HOSTILE_TITLE,
            name=f"bericht-{run}.pdf",
            content=pdf_report,
            mime="application/pdf",
        )
        accepted = main_area(page).get_by_text("Upload angenommen", exact=False)
        accepted.wait_for()
        first_state = ["queued"] if STATUS_LABELS["queued"] in accepted.inner_text() else []
        observed = first_state + observe_job(page, HOSTILE_TITLE, "completed")
        states_seen.update(observed)
        result.metrics["upload_states_observed"] = observed
        result.check(
            "upload_verified", "completed" in observed and jobs_for_hash(env, pdf_report) == 1
        )
        result.check("hostile_title_rendered_literally", xss_absent(page))
        session.screenshot(page, "upload-ready")
        page.reload()
        page.get_by_text("Angemeldet als").first.wait_for()
        navigate(page, "Wissensbasis")
        job_row(page, HOSTILE_TITLE).first.wait_for()
        result.check(
            "refresh_preserves_job_state",
            STATUS_LABELS["completed"] in job_row(page, HOSTILE_TITLE).first.inner_text(),
        )

    pdf_double = synthetic_pdf(f"RC E2E Doppel {run}")
    with Journey(session, "duplicate_submit", page):
        double_title = f"Doppelklick {run}"
        upload_via_ui(
            page,
            title=double_title,
            name=f"doppel-{run}.pdf",
            content=pdf_double,
            mime="application/pdf",
            double=True,
        )
        states_seen.update(observe_job(page, double_title, "completed"))
        upload_via_ui(
            page,
            title=double_title,
            name=f"doppel-{run}.pdf",
            content=pdf_double,
            mime="application/pdf",
        )
        main_area(page).get_by_text("bereits", exact=False).first.wait_for()
        result.metrics["duplicate_jobs"] = max(0, jobs_for_hash(env, pdf_double) - 1)
        result.check(
            "duplicate_submit_single_job",
            jobs_for_hash(env, pdf_double) == 1 and job_row(page, double_title).count() == 1,
        )

    with Journey(session, "upload_rejections", page):
        malformed = b"%not-a-pdf " + run.encode()
        upload_via_ui(
            page, title="Defekt", name="defekt.pdf", content=malformed, mime="application/pdf"
        )
        main_area(page).get_by_text("beschädigt oder passt nicht", exact=False).first.wait_for()
        empty_name = f"leer-{run}.txt"
        upload_via_ui(page, title="Leer", name=empty_name, content=b"", mime="text/plain")
        main_area(page).get_by_text("leer", exact=False).first.wait_for()
        unsupported = b"MZ" + run.encode()
        upload_via_ui(
            page,
            title="Programm",
            name="werkzeug.exe",
            content=unsupported,
            mime="application/octet-stream",
            expect_listed=False,
        )
        page.wait_for_timeout(1000)
        rejected_text = main_area(page).inner_text()
        result.metrics["unsupported_upload_ui_message"] = _sanitize(
            next(
                (
                    line
                    for line in rejected_text.splitlines()
                    if "nicht" in line.lower() or "not allowed" in line.lower()
                ),
                "",
            )
        )
        result.check(
            "malformed_or_unsupported_upload_rejected",
            versions_with_hash(env, malformed) == 0
            and versions_with_hash(env, unsupported) == 0
            and versions_with_hash(env, b"") == 0,
        )
        result.check("upload_rejection_ui_actionable", "Traceback" not in rejected_text)
        session.screenshot(page, "upload-rejections")

    pdf_outage = synthetic_pdf(f"RC E2E Ausfall {run}")
    pdf_viewer = synthetic_pdf(f"RC E2E Ausfall Leser {run}")
    outage_title, viewer_title = f"Ausfall {run}", f"Ausfall Leser {run}"
    with Journey(session, "outage_recovery", page):
        env.queue_proxy.set_mode("refuse")
        try:
            for title, content in ((outage_title, pdf_outage), (viewer_title, pdf_viewer)):
                upload_via_ui(
                    page,
                    title=title,
                    name=f"{title.lower().replace(' ', '-')}.pdf",
                    content=content,
                    mime="application/pdf",
                )
                main_area(page).get_by_text(OUTAGE_TEXT, exact=False).first.wait_for()
            navigate(page, "System / Operations")
            main_area(page).get_by_text("Readiness: eingeschränkt", exact=False).wait_for()
            open_sidebar(page).get_by_text("Dienst eingeschränkt", exact=False).wait_for()
            result.check("readiness_error_ui_verified", True)
            session.screenshot(page, "dependency-outage")
        finally:
            env.queue_proxy.set_mode("forward")

        def ready_again() -> bool:
            navigate(page, "Wissensbasis")
            return bool(open_sidebar(page).get_by_text("API betriebsbereit", exact=False).count())

        recovered = wait_until(ready_again, 20, 1.0)
        result.check("recovery_ui_verified", recovered)
        job_row(page, outage_title).first.wait_for()
        result.check(
            "failed_job_visible_after_outage",
            STATUS_LABELS["failed"] in job_row(page, outage_title).first.inner_text()
            and STATUS_LABELS["failed"] in job_row(page, viewer_title).first.inner_text(),
        )
        choose_option(page, "Fehlgeschlagener Auftrag", outage_title)
        main_area(page).get_by_role("button", name="Erneut versuchen").click()
        main_area(page).get_by_text("Auftrag erneut eingereiht", exact=False).wait_for()
        retried = observe_job(page, outage_title, "completed")
        states_seen.update(["failed", *retried])
        result.metrics["retry_states_observed"] = ["failed", *retried]
        result.check("retry_verified", "completed" in retried)
        session.screenshot(page, "retry-ready")

    with Journey(session, "direct_urls_and_spoofing", page):
        session.allow(
            r"/v1/(admin/finops/summary|ingestion/jobs) 401$",
            "direct API URL without a bearer token is denied",
        )
        session.allow(
            r"status of 401 \(Unauthorized\) @ http://127\.0\.0\.1:\d+/v1/"
            r"(admin/finops/summary|ingestion/jobs)$",
            "browser console reports the expected 401 of the direct API URL",
        )
        session.allow(
            r"status of 404 \(Not Found\) @ http://127\.0\.0\.1:\d+/favicon\.ico$",
            "the JSON API has no favicon; requested by the browser on direct navigation",
        )
        denied = [
            page.goto(f"{env.api_url}{path}")
            for path in ("/v1/admin/finops/summary", "/v1/ingestion/jobs")
        ]
        result.check(
            "direct_url_denied",
            all(response is not None and response.status == 401 for response in denied)
            and "spend" not in page.content().lower(),
        )
        page.goto(f"{env.ui_url}/?tenant_id={TENANT_B}&tenant={TENANT_B}&role=admin")
        page.get_by_text("Angemeldet als").first.wait_for()
        sidebar_text = open_sidebar(page).inner_text()
        result.check(
            "ui_parameter_spoof_ignored",
            "Atlas Industrial" in sidebar_text and "Orion" not in sidebar_text,
        )
        spoofed_jobs = env.api("/v1/ingestion/jobs", "a-admin", headers={"X-Tenant-ID": TENANT_B})[
            1
        ]
        header_leak = sum(1 for job in spoofed_jobs or [] if job.get("job_id") == b_job)
        cross_job = env.api(f"/v1/ingestion/jobs/{b_job}", "a-admin")[0]
        body = env.api(
            "/v1/query",
            "a-admin",
            method="POST",
            data={
                "question": QUESTION_A,
                "tenant_id": TENANT_B,
                "role": "admin",
                "user_id": "x",
                "top_k": 5,
            },
        )[1]
        body_leak = sum(
            1 for item in (body or {}).get("citations", []) if item.get("tenant_id") != TENANT_A
        )
        cross_collections = env.api(f"/v1/collections?workspace_id={ws_b}", "a-admin")
        param_workspaces = env.api(f"/v1/workspaces?tenant_id={TENANT_B}", "a-admin")[1]
        param_leak = sum(1 for item in param_workspaces or [] if item.get("tenant_id") != TENANT_A)
        collection_leak = len(cross_collections[1] or []) if cross_collections[0] == 200 else 0
        leaks = header_leak + body_leak + param_leak + collection_leak + (cross_job != 404)
        result.tenant_leakage += leaks
        result.check("tenant_spoof_rejected", leaks == 0)
        dom = page.content()
        result.tenant_leakage += sum(marker in dom for marker in b_markers)

    ctx_v, page_v = session.context(DESKTOP)
    with Journey(session, "viewer_rbac", page_v) as journey:
        login(session, page_v, env, "a-viewer")
        journey.trace(ctx_v)
        side = open_sidebar(page_v).inner_text()
        result.check("viewer_context_loaded", "Lesezugriff" in side and "Atlas Industrial" in side)
        navigate(page_v, "FinOps")
        main_area(page_v).get_by_text(PERMISSION_TEXT, exact=False).first.wait_for()
        navigate(page_v, "Monitoring")
        main_area(page_v).get_by_text(PERMISSION_TEXT, exact=False).first.wait_for()
        session.screenshot(page_v, "viewer-denied")
        navigate(page_v, "Wissensbasis")
        job_row(page_v, viewer_title).first.wait_for()
        choose_option(page_v, "Fehlgeschlagener Auftrag", viewer_title)
        main_area(page_v).get_by_role("button", name="Erneut versuchen").click()
        main_area(page_v).get_by_text(PERMISSION_TEXT, exact=False).first.wait_for()
        still_failed = db_scalar(
            env,
            "SELECT count(*) FROM ingestion_jobs j JOIN document_versions v "
            "ON v.tenant_id = j.tenant_id AND v.id = j.document_version_id "
            "WHERE v.content_hash = :hash AND j.status = 'failed'",
            hash=hashlib.sha256(pdf_viewer).hexdigest(),
        )
        result.check("rbac_ui_verified", int(still_failed or 0) == 1)
        viewer_job = str(
            db_scalar(
                env,
                "SELECT j.id FROM ingestion_jobs j JOIN document_versions v "
                "ON v.tenant_id = j.tenant_id AND v.id = j.document_version_id "
                "WHERE v.content_hash = :hash",
                hash=hashlib.sha256(pdf_viewer).hexdigest(),
            )
        )
        backend = [
            env.api("/v1/admin/finops/summary", "a-viewer")[0],
            env.api("/v1/admin/models", "a-viewer")[0],
            env.api("/v1/audit-events", "a-viewer")[0],
            env.api("/v1/workspaces", "a-viewer", method="POST", data={"name": "x"})[0],
            env.api(f"/v1/ingestion/jobs/{viewer_job}/retry", "a-viewer", method="POST")[0],
        ]
        result.metrics["viewer_backend_statuses"] = backend
        result.check("backend_rbac_denied", all(status == 403 for status in backend))
        dom = page_v.content()
        result.tenant_leakage += sum(marker in dom for marker in b_markers)
    ctx_v.close()

    session.allow(
        r"^Recording error: Error: Container not found\s+at b\.parentFromOptionsContainer "
        r"\(http://localhost:\d+/static/js/wavesurfer\.esm\.",
        "Streamlit 1.60 framework defect: st.chat_input always initialises its audio "
        "recorder (wavesurfer) even with accept_audio=False; on narrow viewports the "
        "recorder container is absent. The mobile chat is asserted to work regardless.",
    )
    ctx_b, page_b = session.context(MOBILE)
    with Journey(session, "tenant_b_mobile", page_b) as journey:
        login(session, page_b, env, "b-admin")
        journey.trace(ctx_b)
        side = open_sidebar(page_b).inner_text()
        result.check("tenant_b_context_loaded", "Orion Health Systems" in side)
        navigate(page_b, "Wissensbasis")
        collapse_sidebar(page_b)
        if not wait_until(lambda: ws_name_b in page_b.content(), 10):
            raise TimeoutError("tenant B workspace not shown")
        job_row(page_b, b_title).first.wait_for()
        dom = page_b.content()
        leaked = sum(marker in dom for marker in a_markers) + (HOSTILE_TITLE in dom)
        result.tenant_leakage += leaked
        result.check("tenant_b_isolated_ui", leaked == 0)
        responsive_mobile.append(no_horizontal_overflow(page_b))
        session.screenshot(page_b, "tenant-b-mobile-knowledge")
        navigate(page_b, "Copilot")
        collapse_sidebar(page_b)
        responsive_mobile.append(
            no_horizontal_overflow(page_b)
            and unobstructed(page_b, page_b.get_by_test_id("stChatInputTextArea"))
        )
        # Tenant A's answerable question must not surface tenant A sources for tenant B.
        ask(page_b, QUESTION_A)
        main_area(page_b).get_by_text(ABSTENTION_TEXT, exact=False).first.wait_for()
        result.check(
            "tenant_b_cannot_retrieve_tenant_a",
            main_area(page_b).locator(".source-title").count() == 0,
        )
        ask(page_b, QUESTION_B)
        shown = source_titles(page_b)
        result.tenant_leakage += len(set(shown) & (titles_a - titles_b))
        result.check("tenant_b_query_sources_isolated", bool(shown) and set(shown) <= titles_b)
        responsive_mobile.append(no_horizontal_overflow(page_b))
        session.screenshot(page_b, "tenant-b-mobile-answer")
        api_jobs = env.api("/v1/ingestion/jobs", "b-admin")[1] or []
        result.tenant_leakage += sum(
            1 for job in api_jobs if job.get("title") in {HOSTILE_TITLE, f"Doppelklick {run}"}
        )
    ctx_b.close()

    with Journey(session, "rate_limit") as journey:
        token = env.token("a-support")
        retry_after = None
        statuses: list[int] = []
        for _ in range(RATE_LIMIT + 5):
            status, _, headers = http_json(
                f"{env.api_url}/v1/query",
                "POST",
                {"question": "Welche aktuellen Preisregeln gelten?", "top_k": 2},
                token=token,
            )
            statuses.append(status)
            if status == 429:
                retry_after = {key.lower(): value for key, value in headers.items()}.get(
                    "retry-after"
                )
                break
        result.metrics["rate_limit_retry_after_seconds"] = retry_after
        result.metrics["rate_limit_prefill_statuses"] = sorted(set(statuses))
        ctx_rl, page_rl = session.context(DESKTOP)
        journey.page = page_rl
        try:
            login(session, page_rl, env, "a-support")
            ask(page_rl, "Welche aktuellen Preisregeln gelten für Enterprise-Verträge?")
            main_area(page_rl).get_by_text(
                "Zu viele Anfragen. Bitte in", exact=False
            ).first.wait_for()
            text = main_area(page_rl).inner_text()
            result.check(
                "rate_limit_ui_verified",
                retry_after is not None and "Sekunden erneut versuchen" in text,
            )
            session.screenshot(page_rl, "rate-limited")
        finally:
            ctx_rl.close()

    ctx_k, page_k = session.context(DESKTOP)
    with Journey(session, "keyboard", page_k) as journey:
        page_k.goto(env.ui_url)
        main_area(page_k).get_by_role("heading", name="Anmeldung erforderlich").wait_for()
        focus_log: list[dict[str, Any]] = []
        for _ in range(30):
            page_k.keyboard.press("Tab")
            focused = focus_indicator(page_k)
            focus_log.append(focused)
            if "Anmelden" in str(focused.get("name")):
                break
        login_focus = focus_log[-1]
        page_k.keyboard.press("Enter")
        page_k.wait_for_url(f"{KEYCLOAK_URL}/**")
        page_k.locator("#username").wait_for()
        if (
            page_k.evaluate("() => document.activeElement && document.activeElement.id")
            != "username"
        ):
            page_k.locator("#username").focus()
        viewer = env.users["a-viewer"]
        page_k.keyboard.type(viewer.username)
        page_k.keyboard.press("Tab")
        page_k.keyboard.type(viewer.password)
        page_k.keyboard.press("Enter")
        page_k.wait_for_url(f"{env.ui_url}/**")
        page_k.get_by_text("Angemeldet als").first.wait_for()
        journey.trace(ctx_k)
        path: list[dict[str, Any]] = []
        for _ in range(80):
            page_k.keyboard.press("Tab")
            focused = focus_indicator(page_k)
            path.append(focused)
            if focused.get("testid") == "stChatInputTextArea" or (
                focused.get("tag") == "textarea" and "stChatInput" in str(focused.get("testid"))
            ):
                break
        reached = bool(path) and "stChatInput" in str(path[-1].get("testid"))
        messages = page_k.get_by_test_id("stChatMessage")
        before = messages.count()
        page_k.keyboard.type(QUESTION_A)
        page_k.keyboard.press("Enter")
        # The viewer role may receive a governed abstention; either reply proves the flow.
        messages.nth(before + 1).wait_for()
        distinct = {(item.get("tag"), item.get("testid"), item.get("name")) for item in path}
        invisible = [item for item in path if not item.get("visible")]
        result.metrics["keyboard_tab_stops_to_chat"] = len(path)
        result.metrics["keyboard_stops_without_visible_focus"] = [
            f"{item.get('tag')}:{item.get('testid')}" for item in invisible
        ]
        result.check(
            "keyboard_login_handoff",
            bool(login_focus.get("visible")) and "Anmelden" in str(login_focus.get("name")),
        )
        result.check("keyboard_flow_verified", reached and len(distinct) >= 3)
        result.check(
            "focus_visible_on_critical_controls",
            bool(path[-1].get("visible")) and bool(login_focus.get("visible")),
        )
    ctx_k.close()

    with Journey(session, "session_expiry") as journey:
        client_uuid = env.admin.client_uuid(env.client_id)
        _, representation = env.admin.call("GET", f"/clients/{client_uuid}")
        attributes = dict(representation.get("attributes", {}))
        env.admin.call(
            "PUT",
            f"/clients/{client_uuid}",
            {**representation, "attributes": {**attributes, "access.token.lifespan": "15"}},
        )
        ctx_e, page_e = session.context(DESKTOP)
        try:
            login(session, page_e, env, "a-viewer")
            journey.trace(ctx_e)
            logged_in_at = time.monotonic()
            page_e.wait_for_timeout(max(0, int((17 - (time.monotonic() - logged_in_at)) * 1000)))
            open_sidebar(page_e).get_by_role("button", name="Neue Unterhaltung").click()
            main_area(page_e).get_by_role("heading", name="Sitzung abgelaufen").wait_for()
            text = page_e.locator("body").inner_text()
            result.check(
                "session_expiry_verified",
                "Angemeldet als" not in text and "Guten Tag" not in text and "Atlas" not in text,
            )
            session.screenshot(page_e, "session-expired")
            main_area(page_e).get_by_role("button", name="Erneut anmelden").click()
            page_e.wait_for_url(
                re.compile(rf"^({re.escape(KEYCLOAK_URL)}|{re.escape(env.ui_url)})")
            )
            if page_e.url.startswith(KEYCLOAK_URL) and page_e.locator("#username").count():
                keycloak_login(page_e, env.users["a-viewer"])
            page_e.wait_for_url(f"{env.ui_url}/**")
            page_e.get_by_text("Angemeldet als").first.wait_for()
            result.check("session_relogin_verified", True)
        finally:
            ctx_e.close()
            env.admin.call(
                "PUT", f"/clients/{client_uuid}", {**representation, "attributes": attributes}
            )

    ctx_f, page_f = session.context(DESKTOP)
    with Journey(session, "auth_failures", page_f):
        session.allow(
            r"login-actions/authenticate.* (400|401)$", "Keycloak rejects the wrong password"
        )
        page_f.goto(env.ui_url)
        page_f.get_by_role("button", name="Anmelden").click()
        keycloak_login(page_f, env.users["a-viewer"], password="wrong-" + secrets.token_hex(4))
        page_f.locator("#input-error, .kc-feedback-text, [id*=error]").first.wait_for()
        keycloak_text = page_f.locator("body").inner_text()
        result.check(
            "wrong_password_safe",
            page_f.url.startswith(KEYCLOAK_URL)
            and "Exception" not in keycloak_text
            and "Traceback" not in keycloak_text,
        )
        page_f.goto(f"{env.ui_url}/oauth2callback?code=forged-{run}&state=forged-{run}")
        main_area(page_f).get_by_role("heading", name="Anmeldung erforderlich").wait_for()
        body_text = page_f.locator("body").inner_text()
        result.check(
            "rejected_callback_safe",
            "Angemeldet als" not in body_text
            and "Traceback" not in body_text
            and "Exception" not in body_text,
        )
        page_f.goto(env.ui_idp_down_url)
        main_area(page_f).get_by_text(
            "Die Anmeldung ist vorübergehend nicht verfügbar", exact=False
        ).wait_for()
        result.check(
            "idp_unavailable_safe",
            page_f.get_by_role("button", name="Anmelden").count() == 0,
        )
        session.screenshot(page_f, "identity-provider-unavailable")
    ctx_f.close()

    with Journey(session, "logout", page):
        page.goto(env.ui_url)
        page.get_by_text("Angemeldet als").first.wait_for()
        open_sidebar(page).get_by_role("button", name="Abmelden").click()
        page.wait_for_url(f"{env.ui_url}/**", timeout=UI_TIMEOUT_MS)
        main_area(page).get_by_role("heading", name="Anmeldung erforderlich").wait_for()
        cookies_cleared = not any(
            cookie["name"].startswith("_streamlit_user") for cookie in ctx_a.cookies()
        )
        page.go_back()
        page.wait_for_timeout(1500)
        back_text = page.locator("body").inner_text()
        page.goto(env.ui_url)
        main_area(page).get_by_role("heading", name="Anmeldung erforderlich").wait_for()
        page.get_by_role("button", name="Anmelden").click()
        page.wait_for_url(f"{KEYCLOAK_URL}/**")
        idp_session_ended = page.locator("#username").is_visible()
        result.check("logout_verified", cookies_cleared and idp_session_ended)
        result.check(
            "logout_back_navigation_blocked",
            "Angemeldet als" not in back_text and "Guten Tag" not in back_text,
        )
    ctx_a.close()

    critical = sum(
        1 for item in axe for violation in item["violations"] if violation["impact"] == "critical"
    )
    result.metrics["axe"] = axe
    result.metrics["axe_critical_violations"] = critical
    naming_rules = {
        "button-name",
        "label",
        "link-name",
        "input-button-name",
        "select-name",
        "aria-input-field-name",
    }
    result.check(
        "accessible_names_verified",
        not any(v["id"] in naming_rules for item in axe for v in item["violations"]),
    )
    result.check("axe_no_critical_violations", critical == 0 and len(axe) >= 3)
    result.check("responsive_desktop", bool(responsive_desktop) and all(responsive_desktop))
    result.check("responsive_mobile", bool(responsive_mobile) and all(responsive_mobile))
    result.metrics["ingestion_states_observed"] = sorted(states_seen)
    result.check(
        "ingestion_states_observed",
        {"queued", "completed", "failed"} <= states_seen or {"completed", "failed"} <= states_seen,
    )
    evidence["browser_version"] = session.version


def collapse_sidebar(page: Any) -> None:
    """On narrow screens the sidebar overlays content; close it after navigating."""
    if page.viewport_size and page.viewport_size["width"] < 768:
        button = page.get_by_test_id("stSidebarCollapseButton")
        with suppress(Exception):
            if button.is_visible():
                button.click()


# --------------------------------------------------------------------------- verdict


REQUIRED = (
    "migration_at_head",
    "support_tenant_corpora_disjoint",
    "journey_login_completed",
    "unauthenticated_shows_only_login",
    "keycloak_redirect_verified",
    "real_browser_login_verified",
    "return_path_preserved",
    "tenant_role_context_loaded",
    "session_cookie_http_only",
    "direct_reload_keeps_session",
    "journey_navigation_admin_completed",
    "navigation_all_pages",
    "admin_pages_succeed",
    "journey_query_completed",
    "query_answer_rendered",
    "citations_or_sources_verified",
    "hostile_query_rendered_literally",
    "journey_upload_completed",
    "upload_ui_present",
    "upload_verified",
    "hostile_title_rendered_literally",
    "refresh_preserves_job_state",
    "journey_duplicate_submit_completed",
    "duplicate_submit_single_job",
    "journey_upload_rejections_completed",
    "malformed_or_unsupported_upload_rejected",
    "upload_rejection_ui_actionable",
    "journey_outage_recovery_completed",
    "readiness_error_ui_verified",
    "recovery_ui_verified",
    "failed_job_visible_after_outage",
    "retry_verified",
    "journey_direct_urls_and_spoofing_completed",
    "direct_url_denied",
    "ui_parameter_spoof_ignored",
    "tenant_spoof_rejected",
    "journey_viewer_rbac_completed",
    "viewer_context_loaded",
    "rbac_ui_verified",
    "backend_rbac_denied",
    "journey_tenant_b_mobile_completed",
    "tenant_b_context_loaded",
    "tenant_b_isolated_ui",
    "tenant_b_cannot_retrieve_tenant_a",
    "tenant_b_query_sources_isolated",
    "journey_rate_limit_completed",
    "rate_limit_ui_verified",
    "journey_keyboard_completed",
    "keyboard_login_handoff",
    "keyboard_flow_verified",
    "focus_visible_on_critical_controls",
    "journey_session_expiry_completed",
    "session_expiry_verified",
    "session_relogin_verified",
    "journey_auth_failures_completed",
    "wrong_password_safe",
    "rejected_callback_safe",
    "idp_unavailable_safe",
    "journey_logout_completed",
    "logout_verified",
    "logout_back_navigation_blocked",
    "accessible_names_verified",
    "axe_no_critical_violations",
    "responsive_desktop",
    "responsive_mobile",
    "ingestion_states_observed",
    "processes_stopped",
    "keycloak_gate_objects_removed",
    "unrelated_redis_sentinel_unchanged",
    "redis_gate_keys_removed",
    "unrelated_redis_data_unchanged",
    "gate_database_removed",
    "unrelated_postgres_data_unchanged",
    "unrelated_files_unchanged",
    "disposable_qdrant_removed",
    "no_page_errors",
    "no_severe_console_errors",
    "no_unexpected_failed_requests",
    "no_sensitive_artifacts",
)


def classify(result: GateResult) -> tuple[str, str]:
    missing = [name for name in REQUIRED if name not in result.checks]
    failed = sorted(name for name, ok in result.checks.items() if not ok)
    if result.paid_provider_calls:
        return "FAIL", "a paid provider was constructed during the browser run"
    if result.tenant_leakage:
        return "FAIL", "tenant data reached another tenant's browser session"
    if failed or missing:
        return "FAIL", f"browser e2e invariants failed: {', '.join(failed + missing)}"
    return "PASS", "a real browser logged in through Keycloak and completed every journey safely"


def prior_gate_state() -> dict[str, str]:
    try:
        data = json.loads((RC_LIVE / "final-release-gate.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    gates = data.get("mandatory_gates", {})
    return {str(key): str(value) for key, value in gates.items()} if isinstance(gates, dict) else {}


def build_evidence(result: GateResult, evidence: dict[str, Any]) -> dict[str, Any]:
    status, reason = classify(result)
    ok, m = result.checks.get, result.metrics
    prior = prior_gate_state()
    preserved = all(prior.get(gate) == "PASS" for gate in PRIOR_PASS_GATES)
    return {
        "status": status,
        "reason": reason,
        "browser_engine": "chromium",
        "browser_version": evidence.get("browser_version"),
        "browser_framework": "playwright 1.63.0 (python), pinned Chromium build",
        "headless": True,
        "viewport_desktop": f"{DESKTOP['width']}x{DESKTOP['height']}",
        "viewport_mobile": f"{MOBILE['width']}x{MOBILE['height']}",
        "frontend_framework": "streamlit 1.60.0 (apps/dashboard/dashboard.py)",
        "ui_start_command_sanitized": evidence.get("ui_start_command_sanitized"),
        "ui_routes_discovered": [
            "/ (single Streamlit page; sidebar views: Copilot, Wissensbasis, Monitoring, "
            "FinOps, Governance & Audit, System / Operations)",
            "/auth/login",
            "/oauth2callback",
            "/auth/logout",
            "/_stcore/health",
        ],
        "api_backend": "FastAPI via uvicorn (apps/api/main.py), "
        "called server-side by the dashboard",
        "oidc_provider": f"Keycloak 25 local realm {REALM}",
        "auth_flow": "authorization code with PKCE (S256) via Streamlit st.login; "
        "confidential gate-owned client; access token forwarded as bearer to the API",
        "real_browser_login_verified": bool(
            ok("real_browser_login_verified") and ok("keycloak_redirect_verified")
        ),
        "return_path": "/ (the dashboard has one route; Streamlit returns to it after login)",
        "logout_verified": bool(ok("logout_verified") and ok("logout_back_navigation_blocked")),
        "session_expiry_verified": bool(
            ok("session_expiry_verified") and ok("session_relogin_verified")
        ),
        "session_expiry_behavior": "expired access token -> 'Sitzung abgelaufen' without data; "
        "re-login through Keycloak restores the session (no silent refresh-token flow)",
        "roles_tested": ["admin", "viewer", "support"],
        "tenants_tested": [TENANT_A, TENANT_B],
        "rbac_ui_verified": bool(ok("rbac_ui_verified")),
        "direct_url_denied": bool(ok("direct_url_denied") and ok("ui_parameter_spoof_ignored")),
        "backend_rbac_denied": bool(ok("backend_rbac_denied")),
        "viewer_backend_statuses": m.get("viewer_backend_statuses"),
        "browser_e2e_tenant_leakage": result.tenant_leakage,
        "upload_ui_present": bool(ok("upload_ui_present")),
        "upload_verified": bool(ok("upload_verified")),
        "ingestion_states_observed": m.get("ingestion_states_observed"),
        "upload_states_observed": m.get("upload_states_observed"),
        "retry_states_observed": m.get("retry_states_observed"),
        "retry_verified": bool(ok("retry_verified")),
        "duplicate_jobs": m.get("duplicate_jobs"),
        "query_ui_present": True,
        "query_verified": bool(ok("query_answer_rendered")),
        "browser_query_uses_qdrant": False,
        "query_mode": "demo",
        "query_path_note": "the API runs with RAGOPS_QUERY_MODE=demo, so the Copilot answers "
        "from the local JSON demo corpus; uploaded documents are indexed into Qdrant by the "
        "worker. The vector query path (RAGOPS_QUERY_MODE=vector) is not driven by this gate",
        "citations_or_sources_verified": bool(ok("citations_or_sources_verified")),
        "rate_limit_ui_verified": bool(ok("rate_limit_ui_verified")),
        "rate_limit_retry_after_seconds": m.get("rate_limit_retry_after_seconds"),
        "rate_limit_prefill_statuses": m.get("rate_limit_prefill_statuses"),
        "readiness_error_ui_verified": bool(ok("readiness_error_ui_verified")),
        "recovery_ui_verified": bool(ok("recovery_ui_verified")),
        "unsupported_upload_ui_message": m.get("unsupported_upload_ui_message"),
        "keyboard_flow_verified": bool(
            ok("keyboard_flow_verified") and ok("keyboard_login_handoff")
        ),
        "keyboard_tab_stops_to_chat": m.get("keyboard_tab_stops_to_chat"),
        "keyboard_stops_without_visible_focus": m.get("keyboard_stops_without_visible_focus"),
        "accessible_names_verified": bool(ok("accessible_names_verified")),
        "axe_critical_violations": m.get("axe_critical_violations"),
        "axe_findings": m.get("axe"),
        "responsive_desktop": bool(ok("responsive_desktop")),
        "responsive_mobile": bool(ok("responsive_mobile")),
        "page_errors": len(result.page_errors),
        "severe_console_errors": len(result.console_errors),
        "unexpected_failed_requests": len(result.failed_requests),
        "unexpected_events": {
            "page_errors": result.page_errors,
            "console_errors": result.console_errors,
            "failed_requests": result.failed_requests,
        },
        "expected_failures": result.expected_failures,
        "screenshot_count": len(result.screenshots),
        "screenshots": result.screenshots,
        "trace_on_failure_configured": True,
        "traces_kept": result.traces,
        "sensitive_artifacts_detected": not ok("no_sensitive_artifacts"),
        "provider_invocations": result.provider_invocations,
        "paid_provider_calls": result.paid_provider_calls,
        "cleanup_status": "PASS"
        if all(
            ok(name)
            for name in (
                "processes_stopped",
                "keycloak_gate_objects_removed",
                "redis_gate_keys_removed",
                "gate_database_removed",
                "disposable_qdrant_removed",
            )
        )
        else "FAIL",
        "unrelated_data_unchanged": all(
            ok(name)
            for name in (
                "unrelated_postgres_data_unchanged",
                "unrelated_redis_data_unchanged",
                "unrelated_redis_sentinel_unchanged",
                "unrelated_files_unchanged",
                "keycloak_gate_objects_removed",
            )
        ),
        "previous_pass_gates_preserved": preserved,
        "final_pass_count": sum(1 for gate in PRIOR_PASS_GATES if prior.get(gate) == "PASS")
        + (1 if status == "PASS" else 0),
        "checks": dict(sorted(result.checks.items())),
        "errors": result.errors,
    }


def scan_artifacts(run_dir: Path, secrets_to_find: list[str]) -> list[str]:
    """Return artifact names that contain a credential, token or session value."""
    needles = [value.encode() for value in secrets_to_find if value]
    hits: list[str] = []

    def dirty(blob: bytes) -> bool:
        return any(needle in blob for needle in needles) or bool(
            SECRET_PATTERNS.search(blob.decode("latin-1"))
        )

    for path in sorted(run_dir.rglob("*")):
        if not path.is_file():
            continue
        blob = path.read_bytes()
        if path.suffix == ".zip":
            with suppress(zipfile.BadZipFile), zipfile.ZipFile(path) as archive:
                blob += b"".join(archive.read(name) for name in archive.namelist())
        if dirty(blob):
            hits.append(path.name)
    return hits


def run(result: GateResult, evidence: dict[str, Any]) -> None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise PrerequisiteMissing("playwright is not installed (pip install -e .[e2e])") from exc
    run_dir = ARTIFACTS / str(evidence["run_id"])
    run_dir.mkdir(parents=True, exist_ok=True)
    secrets_seen: list[str] = []
    with environment(result, evidence) as env, sync_playwright() as playwright:
        secrets_seen = [env.client_secret, *(user.password for user in env.users.values())]
        try:
            session = BrowserSession(playwright, result, run_dir)
        except Exception as exc:  # noqa: BLE001 - a missing browser build is a prerequisite
            raise PrerequisiteMissing("Chromium for Playwright is not installed") from exc
        try:
            run_journeys(session, env, evidence)
        finally:
            session.close()
            evidence["cookies_serialized"] = False
            evidence["storage_state_serialized"] = False
    hits = scan_artifacts(run_dir, secrets_seen)
    for name in hits:
        (run_dir / name).unlink(missing_ok=True)
    evidence["sensitive_artifacts_removed"] = hits
    result.check("no_sensitive_artifacts", not hits)
    result.screenshots = [name for name in result.screenshots if name not in hits]
    result.traces = [name for name in result.traces if name not in hits]
    result.check("no_page_errors", not result.page_errors)
    result.check("no_severe_console_errors", not result.console_errors)
    result.check("no_unexpected_failed_requests", not result.failed_requests)
    evidence["artifact_dir"] = str(run_dir.relative_to(ROOT))


def _commit() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=False
    )
    return completed.stdout.strip() or "unknown"


def _finish(evidence: dict[str, Any], status: str, reason: str, exit_code: int) -> int:
    evidence.update(
        {
            "status": status,
            "reason": reason,
            "exit_code": exit_code,
            "timestamp": datetime.now(UTC).isoformat(),
        }
    )
    serialized = json.dumps(evidence, indent=2, default=str) + "\n"
    if SECRET_PATTERNS.search(serialized) or re.search(r"client_secret|password=", serialized):
        evidence = {
            key: evidence.get(key) for key in ("gate", "tested_commit", "run_id", "timestamp")
        }
        evidence.update(
            {
                "status": "FAIL",
                "reason": "evidence secret scan failed",
                "exit_code": 1,
                "secret_scan_passed": False,
                "sensitive_artifacts_detected": True,
            }
        )
        serialized = json.dumps(evidence, indent=2) + "\n"
        exit_code = 1
    EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE.write_text(serialized, encoding="utf-8")
    return exit_code


def main() -> int:
    run_id = uuid4().hex[:10]
    evidence: dict[str, Any] = {
        "gate": "browser_e2e",
        "tested_commit": _commit(),
        "run_id": run_id,
    }
    result = GateResult()
    try:
        run(result, evidence)
    except PrerequisiteMissing as exc:
        return _finish(evidence, "BLOCKED", str(exc), 2)
    except Exception as exc:  # noqa: BLE001 - evidence must not carry raw service detail
        result.errors.append(
            f"gate: {type(exc).__name__}: {_sanitize(str(exc).splitlines()[0] if str(exc) else '')}"
        )
        result.check("gate_completed", False)
    evidence.update(build_evidence(result, evidence))
    evidence["secret_scan_passed"] = True
    status, reason = str(evidence.pop("status")), str(evidence.pop("reason"))
    return _finish(evidence, status, reason, 0 if status == "PASS" else 1)


if __name__ == "__main__":
    raise SystemExit(main())

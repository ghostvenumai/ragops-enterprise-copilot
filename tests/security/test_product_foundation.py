from pathlib import Path

import pytest

from ragops.api.app import create_app
from ragops.auth.rbac import AuthorizationError, require_document_access
from ragops.config.settings import Settings
from ragops.llm.providers import provider_from_env
from ragops.retrieval.hybrid import HybridRetriever
from ragops.security.upload import UnsafeUploadError, safe_filename
from ragops.storage.json_store import JsonRepository
from ragops.storage.models import QueryUser


def test_admin_is_always_scoped_to_own_tenant() -> None:
    user = QueryUser(user_id="synthetic-admin", tenant_id="tenant-alpha", role="admin")
    require_document_access(user, "tenant-alpha", "restricted")
    with pytest.raises(AuthorizationError):
        require_document_access(user, "tenant-beta", "public")
    retriever = HybridRetriever(JsonRepository(Path("data/synthetic")).chunks())
    results = retriever.search("Beta Atlas Orion pricing marketing", user, top_k=1000)
    assert results
    assert all(result.chunk.tenant_id == user.tenant_id for result in results)


def test_production_cannot_start_demo_runtime(tmp_path: Path) -> None:
    output = tmp_path / "should-not-exist"
    with pytest.raises(RuntimeError, match="Production runtime is unavailable"):
        create_app(Settings(environment="production", evidence_dir=output))
    assert not output.exists()


@pytest.mark.parametrize("mode", ["prod", "Production", "staging", "", "unknown"])
def test_invalid_environment_fails_closed(mode: str) -> None:
    with pytest.raises(ValueError, match="RAGOPS_ENV"):
        Settings(environment=mode)


@pytest.mark.parametrize("value", [0, -1, 4001])
def test_invalid_input_limit_rejected(value: int) -> None:
    with pytest.raises(ValueError, match="RAGOPS_MAX_INPUT_CHARS"):
        Settings(max_input_chars=value)


def test_invalid_output_limit_rejected() -> None:
    with pytest.raises(ValueError, match="RAGOPS_MAX_OUTPUT_CHARS"):
        Settings(max_output_chars=6001)


def test_provider_typo_does_not_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAGOPS_LLM_PROVIDER", "openaai")
    with pytest.raises(RuntimeError, match="refusing provider fallback"):
        provider_from_env()
    with pytest.raises(ValueError, match="RAGOPS_LLM_PROVIDER"):
        Settings.from_env()


@pytest.mark.parametrize(
    "name", ["..\\private.txt", "C:\\private.txt", "hello\x00.md", "x\n.md", " x.md", "a" * 256]
)
def test_hostile_upload_names_rejected(name: str) -> None:
    with pytest.raises(UnsafeUploadError):
        safe_filename(name)

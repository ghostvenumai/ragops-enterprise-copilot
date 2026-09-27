"""Runtime settings with environment-variable overrides."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    environment: str = "local"
    data_dir: Path = Path("data/synthetic")
    evidence_dir: Path = Path("evidence")
    llm_provider: str = "deterministic"
    prompt_version: str = "ragops-prompt-v1"
    model: str = "deterministic-ragops-v1"
    max_input_chars: int = 4000
    max_output_chars: int = 6000
    identity_provider: str = "development"
    oidc_issuer: str | None = None
    oidc_audience: str | None = None
    oidc_public_key: str | None = None
    oidc_algorithms: tuple[str, ...] = ("RS256",)
    oidc_tenant_claim: str = "tenant_id"
    oidc_roles_claim: str = "roles"
    development_user_id: str = "demo-user"
    development_tenant_id: str = "tenant-alpha"
    development_roles: tuple[str, ...] = ("sales",)
    redis_url: str | None = None
    queue_name: str = "ragops:ingestion"
    worker_concurrency: int = 1
    job_max_attempts: int = 3
    async_ingestion_required: bool = False
    vector_provider: str = "deterministic"
    qdrant_url: str | None = None
    qdrant_collection: str = "ragops_vectors"
    qdrant_api_key: str | None = None
    vector_schema_version: str = "v1"
    embedding_provider: str = "deterministic"
    embedding_model: str = "deterministic-hash-v1"
    embedding_dimension: int = 64

    def __post_init__(self) -> None:
        if self.environment not in {"local", "development", "test", "demo", "production"}:
            raise ValueError("Unsupported RAGOPS_ENV")
        if self.llm_provider not in {"deterministic", "openai", "azure_openai"}:
            raise ValueError("Unsupported RAGOPS_LLM_PROVIDER")
        if self.identity_provider not in {"development", "oidc"}:
            raise ValueError("Unsupported RAGOPS_IDENTITY_PROVIDER")
        if not 1 <= self.max_input_chars <= 4000:
            raise ValueError("RAGOPS_MAX_INPUT_CHARS must be between 1 and 4000")
        if not 1 <= self.max_output_chars <= 6000:
            raise ValueError("RAGOPS_MAX_OUTPUT_CHARS must be between 1 and 6000")
        if not 1 <= self.worker_concurrency <= 64:
            raise ValueError("RAGOPS_WORKER_CONCURRENCY must be between 1 and 64")
        if not 1 <= self.job_max_attempts <= 10:
            raise ValueError("RAGOPS_JOB_MAX_ATTEMPTS must be between 1 and 10")
        if self.vector_provider not in {"deterministic", "qdrant"}:
            raise ValueError("Unsupported RAGOPS_VECTOR_PROVIDER")
        if self.embedding_dimension < 1:
            raise ValueError("RAGOPS_EMBEDDING_DIMENSION must be positive")

    def require_supported_runtime(self) -> None:
        """Never expose the demo API by merely setting a production environment."""
        if self.environment == "production":
            raise RuntimeError(
                "Production runtime is unavailable: verified identity, persistent retrieval "
                "and workers must pass product-verify before production can be enabled."
            )

    def validate_identity_configuration(self) -> None:
        if self.identity_provider == "development":
            if self.environment not in {"local", "development", "test", "demo"}:
                raise RuntimeError(
                    "Production runtime is unavailable: Production requires "
                    "RAGOPS_IDENTITY_PROVIDER=oidc"
                )
            return
        if not self.oidc_issuer or not self.oidc_audience or not self.oidc_public_key:
            raise RuntimeError("OIDC requires issuer, audience and public key configuration")
        if not self.oidc_issuer.startswith(("https://", "http://")):
            raise RuntimeError("OIDC issuer must be an absolute HTTP(S) URL")
        if not self.oidc_algorithms or any(
            algorithm not in {"RS256", "RS384", "RS512", "ES256", "ES384", "ES512"}
            for algorithm in self.oidc_algorithms
        ):
            raise RuntimeError("OIDC algorithm configuration is not approved")

    def validate_async_configuration(self) -> None:
        if self.async_ingestion_required and (
            not self.redis_url or not self.redis_url.startswith(("redis://", "rediss://"))
        ):
            raise RuntimeError("Async ingestion requires a valid RAGOPS_REDIS_URL")

    def validate_vector_configuration(self) -> None:
        if self.environment == "production" and self.vector_provider != "qdrant":
            raise RuntimeError("Production requires RAGOPS_VECTOR_PROVIDER=qdrant")
        if self.vector_provider == "qdrant" and (
            not self.qdrant_url or not self.qdrant_url.startswith(("http://", "https://"))
        ):
            raise RuntimeError("Qdrant provider requires a valid RAGOPS_QDRANT_URL")

    def validate_model_configuration(self) -> None:
        if self.environment == "production" and self.llm_provider == "deterministic":
            raise RuntimeError("Production requires an approved external LLM provider")
        if self.llm_provider == "openai":
            missing = [name for name in ("OPENAI_API_KEY", "OPENAI_MODEL") if not os.getenv(name)]
            if missing:
                raise RuntimeError(
                    "OpenAI provider missing required configuration: " + ", ".join(missing)
                )
        if self.llm_provider == "azure_openai":
            required = (
                "AZURE_OPENAI_API_KEY",
                "AZURE_OPENAI_ENDPOINT",
                "AZURE_OPENAI_DEPLOYMENT",
                "AZURE_OPENAI_API_VERSION",
            )
            missing = [name for name in required if not os.getenv(name)]
            if missing:
                raise RuntimeError(
                    f"Azure OpenAI provider missing required configuration: {', '.join(missing)}"
                )

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            environment=os.getenv("RAGOPS_ENV", "local"),
            data_dir=Path(os.getenv("RAGOPS_DATA_DIR", "data/synthetic")),
            evidence_dir=Path(os.getenv("RAGOPS_EVIDENCE_DIR", "evidence")),
            llm_provider=os.getenv("RAGOPS_LLM_PROVIDER", "deterministic"),
            prompt_version=os.getenv("RAGOPS_PROMPT_VERSION", "ragops-prompt-v1"),
            model=os.getenv("RAGOPS_MODEL", "deterministic-ragops-v1"),
            max_input_chars=int(os.getenv("RAGOPS_MAX_INPUT_CHARS", "4000")),
            max_output_chars=int(os.getenv("RAGOPS_MAX_OUTPUT_CHARS", "6000")),
            identity_provider=os.getenv("RAGOPS_IDENTITY_PROVIDER", "development").lower(),
            oidc_issuer=os.getenv("RAGOPS_OIDC_ISSUER"),
            oidc_audience=os.getenv("RAGOPS_OIDC_AUDIENCE"),
            oidc_public_key=(os.getenv("RAGOPS_OIDC_PUBLIC_KEY") or "").replace("\\n", "\n")
            or None,
            oidc_algorithms=tuple(
                filter(None, os.getenv("RAGOPS_OIDC_ALGORITHMS", "RS256").split(","))
            ),
            oidc_tenant_claim=os.getenv("RAGOPS_OIDC_TENANT_CLAIM", "tenant_id"),
            oidc_roles_claim=os.getenv("RAGOPS_OIDC_ROLES_CLAIM", "roles"),
            development_user_id=os.getenv("RAGOPS_DEV_USER_ID", "demo-user"),
            development_tenant_id=os.getenv("RAGOPS_DEV_TENANT_ID", "tenant-alpha"),
            development_roles=tuple(
                filter(None, os.getenv("RAGOPS_DEV_ROLES", "sales").split(","))
            ),
            redis_url=os.getenv("RAGOPS_REDIS_URL"),
            queue_name=os.getenv("RAGOPS_QUEUE_NAME", "ragops:ingestion"),
            worker_concurrency=int(os.getenv("RAGOPS_WORKER_CONCURRENCY", "1")),
            job_max_attempts=int(os.getenv("RAGOPS_JOB_MAX_ATTEMPTS", "3")),
            async_ingestion_required=os.getenv("RAGOPS_ASYNC_INGESTION_REQUIRED", "false").lower()
            in {"1", "true", "yes"},
            vector_provider=os.getenv("RAGOPS_VECTOR_PROVIDER", "deterministic").lower(),
            qdrant_url=os.getenv("RAGOPS_QDRANT_URL"),
            qdrant_collection=os.getenv("RAGOPS_QDRANT_COLLECTION", "ragops_vectors"),
            qdrant_api_key=os.getenv("RAGOPS_QDRANT_API_KEY"),
            vector_schema_version=os.getenv("RAGOPS_VECTOR_SCHEMA_VERSION", "v1"),
            embedding_provider=os.getenv("RAGOPS_EMBEDDING_PROVIDER", "deterministic"),
            embedding_model=os.getenv("RAGOPS_EMBEDDING_MODEL", "deterministic-hash-v1"),
            embedding_dimension=int(os.getenv("RAGOPS_EMBEDDING_DIMENSION", "64")),
        )

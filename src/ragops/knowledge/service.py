"""Domain and repository services for workspaces, collections and documents."""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol, TypeVar
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from ragops.persistence.models import (
    Document,
    DocumentVersion,
    KnowledgeCollection,
    TenantOwned,
    Workspace,
)
from ragops.security.documents import inspect_document
from ragops.security.upload import (
    ALLOWED_EXTENSIONS,
    MAX_FILE_SIZE_BYTES,
    UnsafeUploadError,
    safe_filename,
)

LIFECYCLE_STATES = frozenset(
    {
        "uploaded",
        "validating",
        "ready_for_ingestion",
        "processing",
        "indexed",
        "failed",
        "inactive",
        "superseded",
        "deleted",
    }
)
TRANSITIONS: dict[str, frozenset[str]] = {
    "uploaded": frozenset({"validating", "deleted"}),
    "validating": frozenset({"ready_for_ingestion", "failed"}),
    "ready_for_ingestion": frozenset({"processing", "inactive"}),
    "processing": frozenset({"indexed", "failed"}),
    "indexed": frozenset({"inactive", "processing"}),
    "failed": frozenset({"validating", "inactive"}),
    "inactive": frozenset({"processing", "deleted"}),
    "superseded": frozenset({"deleted"}),
    "deleted": frozenset(),
}
CONTENT_SIGNATURES: dict[str, bytes] = {".pdf": b"%PDF-", ".docx": b"PK\x03\x04"}
ALLOWED_MIME: dict[str, str] = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".csv": "text/csv",
}
Record = TypeVar("Record", bound=TenantOwned)


class KnowledgeAuthorizationError(PermissionError):
    """Resource is absent from the caller's tenant or role scope."""


class DuplicateDocumentError(ValueError):
    """An exact duplicate or conflicting logical document upload was requested."""


class DocumentBlobStore(Protocol):
    def put(self, tenant_id: str, content_hash: str, filename: str, content: bytes) -> str: ...
    def get(self, storage_reference: str) -> bytes: ...
    def delete(self, storage_reference: str) -> None: ...


class LocalDocumentBlobStore:
    """Safe development store; keys are generated and never based on paths."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def put(self, tenant_id: str, content_hash: str, filename: str, content: bytes) -> str:
        suffix = Path(filename).suffix.lower()
        key = f"{hashlib.sha256(f'{tenant_id}:{content_hash}'.encode()).hexdigest()}{suffix}"
        target = (self.root / key).resolve()
        if self.root not in target.parents:
            raise UnsafeUploadError("storage path escaped configured root")
        # Untrusted bytes are stored owner-only and never executable.
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
        os.chmod(target, 0o600)
        return key

    def get(self, storage_reference: str) -> bytes:
        target = (self.root / Path(storage_reference).name).resolve()
        if self.root not in target.parents:
            raise UnsafeUploadError("invalid storage reference")
        return target.read_bytes()

    def delete(self, storage_reference: str) -> None:
        target = (self.root / Path(storage_reference).name).resolve()
        if self.root in target.parents and target.exists():
            target.unlink()


@dataclass(frozen=True)
class UploadResult:
    document: Document
    version: DocumentVersion


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    if not slug:
        raise ValueError("name must produce a non-empty slug")
    return slug[:120]


def validate_upload(
    filename: str, mime_type: str, content: bytes, max_size: int = MAX_FILE_SIZE_BYTES
) -> str:
    normalized = safe_filename(filename)
    suffix = Path(normalized).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS or ALLOWED_MIME.get(suffix) != mime_type:
        raise UnsafeUploadError("unsupported file type or MIME type")
    if not content:
        raise UnsafeUploadError("empty files are not allowed")
    if len(content) > max_size:
        raise UnsafeUploadError("file exceeds size limit")
    # Binary formats must carry their signature; a renamed or corrupted file is rejected
    # at intake instead of being reported as successfully ingested.
    signature = CONTENT_SIGNATURES.get(suffix)
    if signature is not None and not content.startswith(signature):
        raise UnsafeUploadError("file content does not match its type")
    inspect_document(suffix, content)
    return normalized


class KnowledgeManagementService:
    def __init__(
        self, session: Session, tenant_id: str, actor: str, blob_store: DocumentBlobStore
    ) -> None:
        if not tenant_id or not actor:
            raise ValueError("tenant and actor are required")
        self.session, self.tenant_id, self.actor, self.blob_store = (
            session,
            tenant_id,
            actor,
            blob_store,
        )

    def create_workspace(self, name: str, description: str = "") -> Workspace:
        workspace = Workspace(
            tenant_id=self.tenant_id,
            name=name,
            slug=slugify(name),
            description=description,
            created_by=self.actor,
        )
        self.session.add(workspace)
        self.session.flush()
        return workspace

    def create_collection(
        self,
        workspace_id: UUID,
        name: str,
        description: str = "",
        access_level: str = "internal",
        allowed_roles: list[str] | None = None,
    ) -> KnowledgeCollection:
        workspace = self._get(Workspace, workspace_id)
        collection = KnowledgeCollection(
            tenant_id=self.tenant_id,
            workspace_id=workspace.id,
            name=name,
            slug=slugify(name),
            description=description,
            access_level=access_level,
            allowed_roles=allowed_roles or [],
            created_by=self.actor,
        )
        self.session.add(collection)
        self.session.flush()
        return collection

    def list_workspaces(self, limit: int = 50, offset: int = 0) -> list[Workspace]:
        return list(
            self.session.scalars(
                select(Workspace)
                .where(Workspace.tenant_id == self.tenant_id)
                .order_by(Workspace.created_at, Workspace.id)
                .offset(offset)
                .limit(min(limit, 200))
            )
        )

    def list_collections(
        self, workspace_id: UUID | None = None, limit: int = 50, offset: int = 0
    ) -> list[KnowledgeCollection]:
        query = select(KnowledgeCollection).where(KnowledgeCollection.tenant_id == self.tenant_id)
        if workspace_id is not None:
            query = query.where(KnowledgeCollection.workspace_id == workspace_id)
        return list(
            self.session.scalars(
                query.order_by(KnowledgeCollection.created_at, KnowledgeCollection.id)
                .offset(offset)
                .limit(min(limit, 200))
            )
        )

    def upload(
        self,
        workspace_id: UUID,
        collection_id: UUID,
        title: str,
        logical_document_key: str,
        filename: str,
        mime_type: str,
        content: bytes,
        access_level: str = "internal",
        metadata: dict[str, str] | None = None,
    ) -> UploadResult:
        collection = self._get(KnowledgeCollection, collection_id)
        if collection.workspace_id != workspace_id:
            raise KnowledgeAuthorizationError("resource not found")
        normalized = validate_upload(filename, mime_type, content)
        content_hash = hashlib.sha256(content).hexdigest()
        existing = self.session.scalar(
            select(DocumentVersion)
            .join(Document, Document.id == DocumentVersion.document_id)
            .where(
                DocumentVersion.tenant_id == self.tenant_id,
                DocumentVersion.content_hash == content_hash,
                Document.collection_id == collection_id,
            )
        )
        if existing is not None:
            raise DuplicateDocumentError("duplicate content in collection")
        document = self.session.scalar(
            select(Document).where(
                Document.tenant_id == self.tenant_id,
                Document.collection_id == collection_id,
                Document.logical_document_key == logical_document_key,
            )
        )
        now = datetime.now(UTC)
        if document is None:
            document = Document(
                tenant_id=self.tenant_id,
                workspace_id=workspace_id,
                collection_id=collection_id,
                title=title,
                logical_document_key=logical_document_key,
                access_level=access_level,
                document_metadata=metadata or {},
                created_by=self.actor,
                status="uploaded",
            )
            self.session.add(document)
            self.session.flush()
        else:
            if document.status == "deleted":
                raise ValueError("deleted documents cannot receive new versions")
            document.status = "uploaded"
        version_number = (
            self.session.scalar(
                select(DocumentVersion.version)
                .where(
                    DocumentVersion.tenant_id == self.tenant_id,
                    DocumentVersion.document_id == document.id,
                )
                .order_by(DocumentVersion.version.desc())
            )
            or 0
        ) + 1
        reference = self.blob_store.put(self.tenant_id, content_hash, normalized, content)
        version = DocumentVersion(
            tenant_id=self.tenant_id,
            document_id=document.id,
            version=version_number,
            filename=normalized,
            mime_type=mime_type,
            content=content,
            content_hash=content_hash,
            storage_reference=reference,
            size=len(content),
            ingestion_status="uploaded",
            valid_from=now,
        )
        self.session.add(version)
        self.session.flush()
        previous = self.session.scalar(
            select(DocumentVersion).where(
                DocumentVersion.tenant_id == self.tenant_id,
                DocumentVersion.document_id == document.id,
                DocumentVersion.id != version.id,
                DocumentVersion.valid_to.is_(None),
            )
        )
        if previous is not None:
            previous.valid_to = now
            previous.superseded_by = version.id
            previous.ingestion_status = "superseded"
            document.status = "uploaded"
        document.current_version_id, document.updated_at = version.id, now
        return UploadResult(document, version)

    def get_document(self, document_id: UUID) -> Document:
        return self._get(Document, document_id)

    def list_documents(
        self,
        workspace_id: UUID | None = None,
        collection_id: UUID | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Document]:
        query = select(Document).where(
            Document.tenant_id == self.tenant_id, Document.active.is_(True)
        )
        if workspace_id is not None:
            query = query.where(Document.workspace_id == workspace_id)
        if collection_id is not None:
            query = query.where(Document.collection_id == collection_id)
        if status is not None:
            query = query.where(Document.status == status)
        return list(
            self.session.scalars(
                query.order_by(Document.updated_at.desc(), Document.id)
                .offset(offset)
                .limit(min(limit, 200))
            )
        )

    def versions(self, document_id: UUID) -> list[DocumentVersion]:
        self._get(Document, document_id)
        return list(
            self.session.scalars(
                select(DocumentVersion)
                .where(
                    DocumentVersion.tenant_id == self.tenant_id,
                    DocumentVersion.document_id == document_id,
                )
                .order_by(DocumentVersion.version.desc())
            )
        )

    def change_status(self, document_id: UUID, status: str) -> Document:
        document = self._get(Document, document_id)
        self._transition(document.status, status)
        document.status = status
        if status in {"inactive", "deleted"}:
            document.active = False
        return document

    def update_metadata(self, document_id: UUID, metadata: dict[str, str]) -> Document:
        document = self._get(Document, document_id)
        document.document_metadata = dict(metadata)
        return document

    def _get(self, model: type[Record], record_id: UUID) -> Record:
        result = self.session.scalar(
            select(model).where(model.tenant_id == self.tenant_id, model.id == record_id)
        )
        if result is None:
            raise KnowledgeAuthorizationError("resource not found")
        return result

    @staticmethod
    def _transition(current: str | None, target: str) -> None:
        if target not in LIFECYCLE_STATES or target not in TRANSITIONS.get(
            current or "", frozenset()
        ):
            raise ValueError(f"invalid document lifecycle transition: {current} -> {target}")

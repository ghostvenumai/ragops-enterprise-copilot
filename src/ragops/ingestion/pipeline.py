"""Document ingestion pipeline with metadata extraction and duplicate detection."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

from ragops.ingestion.loaders import load_document_text
from ragops.ingestion.normalization import chunk_text, tokenize
from ragops.security.prompt_injection import has_prompt_injection
from ragops.security.upload import validate_document_path
from ragops.storage.models import DocumentChunk, DocumentMetadata


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_front_matter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---"):
        inline = parse_inline_metadata(text)
        if inline:
            return inline, text
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    metadata: dict[str, str] = {}
    for line in parts[1].splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            metadata[key.strip()] = value.strip().strip("'\"")
    return metadata, parts[2].strip()


def parse_inline_metadata(text: str) -> dict[str, str]:
    first_line = text.splitlines()[0] if text.splitlines() else ""
    metadata: dict[str, str] = {}
    for segment in first_line.split(";"):
        if ":" not in segment:
            continue
        key, value = segment.split(":", 1)
        metadata[key.strip()] = value.strip()
    if {"document_id", "tenant_id", "title"} <= metadata.keys():
        return metadata
    return {}


def parse_date(value: str | None, default: date) -> date:
    if not value:
        return default
    return date.fromisoformat(value)


def ingest_document(
    path: Path,
    base_dir: Path,
    *,
    default_tenant: str = "tenant-alpha",
    chunk_size: int = 120,
    overlap: int = 20,
) -> list[DocumentChunk]:
    safe_path = validate_document_path(path, base_dir)
    raw = load_document_text(safe_path)
    front_matter, body = parse_front_matter(raw)
    content_hash = sha256_text(body)
    document_id = front_matter.get("document_id", content_hash[:16])
    created = parse_date(front_matter.get("created_at"), date(2026, 1, 1))
    valid_from = parse_date(front_matter.get("valid_from"), created)
    valid_until_raw = front_matter.get("valid_until")
    metadata = DocumentMetadata(
        document_id=document_id,
        tenant_id=front_matter.get("tenant_id", default_tenant),
        title=front_matter.get("title", safe_path.stem),
        source_path=str(safe_path.relative_to(base_dir.resolve())),
        source_type=safe_path.suffix.lower().lstrip("."),
        classification=front_matter.get("classification", "knowledge"),
        access_level=front_matter.get("access_level", "internal"),  # type: ignore[arg-type]
        version=front_matter.get("version", "v1"),
        department=front_matter.get("department", "knowledge"),
        created_at=created,
        valid_from=valid_from,
        valid_until=date.fromisoformat(valid_until_raw) if valid_until_raw else None,
        content_hash=content_hash,
        lineage={
            "ingestion": "local-deterministic",
            "metadata_source": "front_matter",
            "synthetic": "true",
        },
    )
    chunks: list[DocumentChunk] = []
    for ordinal, chunk in enumerate(
        chunk_text(body, chunk_size=chunk_size, overlap=overlap), start=1
    ):
        chunks.append(
            DocumentChunk(
                chunk_id=f"{document_id}:c{ordinal}",
                document_id=document_id,
                tenant_id=metadata.tenant_id,
                text=chunk,
                metadata=metadata,
                ordinal=ordinal,
                token_count=len(tokenize(chunk)),
                has_prompt_injection=has_prompt_injection(chunk),
            )
        )
    return chunks


def ingest_directory(base_dir: Path, manifest_path: Path | None = None) -> list[DocumentChunk]:
    chunks: list[DocumentChunk] = []
    for path in sorted(base_dir.rglob("*")):
        if path.is_file() and path.suffix.lower() in {".pdf", ".docx", ".md", ".txt", ".csv"}:
            chunks.extend(ingest_document(path, base_dir))
    if manifest_path:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest = [
            {
                "chunk_id": chunk.chunk_id,
                "document_id": chunk.document_id,
                "tenant_id": chunk.tenant_id,
                "title": chunk.metadata.title,
                "content_hash": chunk.metadata.content_hash,
                "has_prompt_injection": chunk.has_prompt_injection,
            }
            for chunk in chunks
        ]
        manifest_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    return chunks

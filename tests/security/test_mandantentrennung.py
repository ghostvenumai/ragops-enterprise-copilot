from __future__ import annotations

from pathlib import Path

from ragops.retrieval.hybrid import HybridRetriever
from ragops.storage.json_store import JsonRepository
from ragops.storage.models import QueryUser


def test_cross_tenant_retrieval_is_blocked() -> None:
    retriever = HybridRetriever(JsonRepository(Path("data/synthetic")).chunks())
    user = QueryUser(user_id="u1", tenant_id="tenant-alpha", role="sales")

    results = retriever.search("Orion Health Beta Atlas", user, top_k=10)

    assert all(result.chunk.tenant_id == "tenant-alpha" for result in results)
    assert not any("Beta" in result.chunk.metadata.title for result in results)

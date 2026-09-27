"""Optional live Qdrant gate; skipped unless explicitly configured."""

from __future__ import annotations

import os

import pytest

try:
    import qdrant_client as qdrant
except ImportError:  # optional dependency; keep the integration test explicitly skipped
    qdrant = None


@pytest.mark.integration
@pytest.mark.skipif(qdrant is None, reason="install the pinned vector extra for Qdrant integration")
def test_qdrant_health() -> None:
    url = os.getenv("RAGOPS_QDRANT_URL")
    if not url:
        pytest.skip("RAGOPS_QDRANT_URL is not configured")
    assert qdrant.QdrantClient(url=url).get_collections() is not None

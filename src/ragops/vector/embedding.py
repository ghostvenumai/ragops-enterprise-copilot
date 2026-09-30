"""Embedding provider boundary with deterministic local implementation."""

from __future__ import annotations

import hashlib
import math
from typing import Any, Protocol

from ragops.ingestion.normalization import tokenize

# The model identifier is stored with every chunk; queries must use the same one.
EMBEDDING_MODEL = "deterministic-hash-v1"
EMBEDDING_PROVIDER = "deterministic"


class EmbeddingProvider(Protocol):
    model: str
    dimension: int

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class DeterministicEmbeddingProvider:
    """Local hashed bag-of-words embedding; no network and no paid provider."""

    model = EMBEDDING_MODEL

    def __init__(self, dimension: int = 64) -> None:
        if dimension < 1:
            raise ValueError("embedding dimension must be positive")
        self.dimension = dimension

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            vector = [0.0] * self.dimension
            # One normalization for documents and queries: NFKC, lower case, word tokens.
            for token in tokenize(text):
                index = (
                    int.from_bytes(hashlib.blake2b(token.encode(), digest_size=8).digest())
                    % self.dimension
                )
                vector[index] += 1.0
            norm = math.sqrt(sum(item * item for item in vector)) or 1.0
            vectors.append([item / norm for item in vector])
        return vectors


def embedding_provider_for(settings: Any) -> EmbeddingProvider:
    """The configured embedding provider; unknown providers or models never fall back."""
    if settings.embedding_provider != EMBEDDING_PROVIDER:
        raise RuntimeError("Unsupported RAGOPS_EMBEDDING_PROVIDER; refusing provider fallback")
    if settings.embedding_model != EMBEDDING_MODEL:
        raise RuntimeError("Unsupported RAGOPS_EMBEDDING_MODEL for the deterministic provider")
    return DeterministicEmbeddingProvider(settings.embedding_dimension)

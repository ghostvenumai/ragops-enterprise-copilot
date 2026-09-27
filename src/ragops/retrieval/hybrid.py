"""Tenant-aware hybrid search and reranking."""

from __future__ import annotations

import hashlib
import math
from collections import Counter
from dataclasses import dataclass

from ragops.auth.rbac import can_access_level
from ragops.ingestion.normalization import tokenize
from ragops.security.prompt_injection import has_prompt_injection
from ragops.storage.models import DocumentChunk, QueryUser, SearchResult

SEARCH_STOPWORDS = {
    "a",
    "an",
    "are",
    "auf",
    "aus",
    "bei",
    "das",
    "der",
    "die",
    "ein",
    "eine",
    "einer",
    "eines",
    "für",
    "fuer",
    "gilt",
    "gelten",
    "im",
    "in",
    "is",
    "ist",
    "mit",
    "oder",
    "sind",
    "the",
    "und",
    "von",
    "was",
    "werden",
    "welche",
    "welcher",
    "welches",
    "what",
    "which",
    "wie",
    "wird",
    "zu",
}


def meaningful_tokens(text: str) -> list[str]:
    """Remove language-level noise before lexical and vector scoring."""
    return [token for token in tokenize(text) if token not in SEARCH_STOPWORDS]


def hashed_vector(tokens: list[str], dimensions: int = 64) -> list[float]:
    vector = [0.0] * dimensions
    for token in tokens:
        index = (
            int.from_bytes(hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest())
            % dimensions
        )
        vector[index] += 1.0
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


def cosine(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True))


@dataclass
class HybridRetriever:
    chunks: list[DocumentChunk]
    bm25_weight: float = 0.65
    vector_weight: float = 0.35
    injection_penalty: float = 0.35

    def __post_init__(self) -> None:
        self._tokenized = {
            chunk.chunk_id: meaningful_tokens(
                f"{chunk.metadata.title} {chunk.metadata.classification} {chunk.text}"
            )
            for chunk in self.chunks
        }
        self._term_document_frequency: Counter[str] = Counter()
        for tokens in self._tokenized.values():
            self._term_document_frequency.update(set(tokens))
        self._vectors = {
            chunk.chunk_id: hashed_vector(self._tokenized[chunk.chunk_id]) for chunk in self.chunks
        }

    def search(
        self,
        query: str,
        user: QueryUser,
        *,
        top_k: int = 5,
        filters: dict[str, str] | None = None,
    ) -> list[SearchResult]:
        filters = filters or {}
        query_tokens = meaningful_tokens(query)
        query_vector = hashed_vector(query_tokens)
        query_terms = Counter(query_tokens)
        results: list[SearchResult] = []
        for chunk in self.chunks:
            if chunk.tenant_id != user.tenant_id:
                continue
            if not can_access_level(user.role, chunk.metadata.access_level):
                continue
            if any(getattr(chunk.metadata, key) != value for key, value in filters.items()):
                continue
            tokens = self._tokenized[chunk.chunk_id]
            bm25_score = self._bm25(query_terms, tokens)
            vector_score = cosine(query_vector, self._vectors[chunk.chunk_id])
            combined = self.bm25_weight * bm25_score + self.vector_weight * vector_score
            reasons: list[str] = ["tenant_filter", "rbac_filter"]
            injection = chunk.has_prompt_injection or has_prompt_injection(chunk.text)
            if injection:
                combined *= self.injection_penalty
                reasons.append("prompt_injection_penalty")
            rerank = self._rerank(query_tokens, chunk, combined)
            if rerank > 0:
                results.append(
                    SearchResult(
                        chunk=chunk,
                        bm25_score=round(bm25_score, 4),
                        vector_score=round(vector_score, 4),
                        combined_score=round(combined, 4),
                        rerank_score=round(rerank, 4),
                        reasons=tuple(reasons),
                    )
                )
        return sorted(results, key=lambda item: item.rerank_score, reverse=True)[:top_k]

    def _bm25(self, query_terms: Counter[str], tokens: list[str]) -> float:
        if not query_terms or not tokens:
            return 0.0
        token_counts = Counter(tokens)
        total_docs = max(len(self.chunks), 1)
        score = 0.0
        for term, query_count in query_terms.items():
            if term not in token_counts:
                continue
            doc_frequency = self._term_document_frequency[term]
            idf = math.log(1 + (total_docs - doc_frequency + 0.5) / (doc_frequency + 0.5))
            score += query_count * token_counts[term] * idf / (len(tokens) ** 0.5)
        return score

    def _rerank(
        self, query_tokens: list[str], chunk: DocumentChunk, combined_score: float
    ) -> float:
        if not query_tokens:
            return 0.0
        chunk_tokens = set(self._tokenized[chunk.chunk_id])
        overlap = len(set(query_tokens) & chunk_tokens) / max(len(set(query_tokens)), 1)
        if overlap == 0:
            return 0.0
        freshness = 0.05 if chunk.metadata.version.lower().startswith("v2") else 0.0
        return combined_score + (0.25 * overlap) + freshness

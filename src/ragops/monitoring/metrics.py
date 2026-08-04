"""In-memory metrics and cost accounting."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class MetricsRegistry:
    request_count: int = 0
    success_count: int = 0
    failure_count: int = 0
    prompt_injection_detections: int = 0
    blocked_access_attempts: int = 0
    total_tokens: int = 0
    total_cost_eur: float = 0.0
    latencies_ms: list[float] = field(default_factory=list)
    retrieval_latencies_ms: list[float] = field(default_factory=list)
    llm_latencies_ms: list[float] = field(default_factory=list)
    citation_coverages: list[float] = field(default_factory=list)

    def record_request(
        self,
        *,
        success: bool,
        latency_ms: float,
        retrieval_latency_ms: float,
        llm_latency_ms: float,
        tokens: int,
        cost_eur: float,
        citation_coverage: float,
    ) -> None:
        self.request_count += 1
        self.success_count += int(success)
        self.failure_count += int(not success)
        self.latencies_ms.append(latency_ms)
        self.retrieval_latencies_ms.append(retrieval_latency_ms)
        self.llm_latencies_ms.append(llm_latency_ms)
        self.total_tokens += tokens
        self.total_cost_eur += cost_eur
        self.citation_coverages.append(citation_coverage)

    def to_dict(self) -> dict[str, float | int]:
        def avg(values: list[float]) -> float:
            return round(sum(values) / len(values), 3) if values else 0.0

        return {
            "request_count": self.request_count,
            "success_count": self.success_count,
            "failure_count": self.failure_count,
            "success_rate": round(self.success_count / self.request_count, 4)
            if self.request_count
            else 0.0,
            "avg_latency_ms": avg(self.latencies_ms),
            "avg_retrieval_latency_ms": avg(self.retrieval_latencies_ms),
            "avg_llm_latency_ms": avg(self.llm_latencies_ms),
            "prompt_injection_detections": self.prompt_injection_detections,
            "blocked_access_attempts": self.blocked_access_attempts,
            "total_tokens": self.total_tokens,
            "total_cost_eur": round(self.total_cost_eur, 6),
            "avg_citation_coverage": avg(self.citation_coverages),
        }

    def write_json(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

"""Executable retrieval and answer evaluation pipeline."""

from __future__ import annotations

import json
from pathlib import Path
from statistics import mean
from time import perf_counter
from typing import NotRequired, TypedDict, cast

from ragops.governance.audit import AuditLogger
from ragops.monitoring.metrics import MetricsRegistry
from ragops.storage.json_store import JsonRepository
from ragops.storage.models import QueryUser, Role
from ragops.workflows.state_machine import QueryRequest, RAGWorkflow


class GoldCase(TypedDict):
    id: str
    category: str
    tenant_id: str
    question: str
    user_id: NotRequired[str]
    role: NotRequired[Role]
    expected_sources: NotRequired[list[str]]
    expect_abstain: NotRequired[bool]


class CaseResult(TypedDict):
    id: str
    category: str
    hit: bool
    expected_abstain: bool
    abstained: bool
    tenant_leakage: bool
    security_pass: bool
    citation_count: int
    evidence_score: float
    latency_ms: float
    tokens: int
    cost_eur: float
    answer_preview: str


def _load_gold(path: Path) -> list[GoldCase]:
    payload: object = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("gold dataset must contain a JSON list")
    return cast(list[GoldCase], payload)


def _matches_expected(citations: list[str], expected: list[str]) -> bool:
    if not expected:
        return True
    haystack = " ".join(citations).lower()
    return any(item.lower() in haystack for item in expected)


def run_evaluation(
    gold_path: Path,
    evidence_dir: Path,
    *,
    data_dir: Path = Path("data/synthetic"),
) -> dict[str, object]:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    repository = JsonRepository(data_dir)
    metrics = MetricsRegistry()
    workflow = RAGWorkflow(
        repository,
        audit_logger=AuditLogger(evidence_dir / "audit-events.jsonl"),
        metrics=metrics,
    )
    cases = _load_gold(gold_path)
    case_results: list[CaseResult] = []
    started = perf_counter()
    for case in cases:
        user = QueryUser(
            user_id=case.get("user_id", "eval-user"),
            tenant_id=case["tenant_id"],
            role=case.get("role", "sales"),
        )
        state = workflow.run(QueryRequest(question=case["question"], user=user, top_k=5))
        citation_ids = [citation.source_id for citation in state.citations]
        expected_sources = case.get("expected_sources", [])
        expected_abstain = case.get("expect_abstain", False)
        hit = _matches_expected(citation_ids, expected_sources)
        tenant_leakage = any(
            citation.tenant_id != user.tenant_id and user.role != "admin"
            for citation in state.citations
        )
        security_case = case["category"].lower() in {
            "cross-tenant-attack",
            "prompt-injection",
            "pii-output",
            "unauthorized-access",
        }
        security_pass = (not tenant_leakage) and (
            state.abstained if expected_abstain or security_case else True
        )
        case_results.append(
            {
                "id": case["id"],
                "category": case["category"],
                "hit": hit,
                "expected_abstain": expected_abstain,
                "abstained": state.abstained,
                "tenant_leakage": tenant_leakage,
                "security_pass": security_pass,
                "citation_count": len(state.citations),
                "evidence_score": state.evidence_score,
                "latency_ms": state.total_latency_ms,
                "tokens": state.token_count,
                "cost_eur": state.estimated_cost_eur,
                "answer_preview": state.answer[:180],
            }
        )
    total = len(case_results)
    retrieval_hits = sum(result["hit"] for result in case_results)
    citation_covered = sum(
        result["abstained"] or result["citation_count"] > 0 for result in case_results
    )
    tenant_leaks = sum(result["tenant_leakage"] for result in case_results)
    security_cases = [
        result
        for result in case_results
        if "attack" in result["category"].lower()
        or "injection" in result["category"].lower()
        or "pii" in result["category"].lower()
        or "unauthorized" in result["category"].lower()
    ]
    security_passes = sum(result["security_pass"] for result in security_cases)
    report: dict[str, object] = {
        "status": "passed"
        if tenant_leaks == 0
        and total > 0
        and (not security_cases or security_passes == len(security_cases))
        else "failed",
        "case_count": total,
        "retrieval_hit_rate": round(retrieval_hits / total, 4) if total else 0.0,
        "recall_at_5": round(retrieval_hits / total, 4) if total else 0.0,
        "precision_at_5": round(
            mean(
                (1.0 if result["hit"] else 0.0) / max(result["citation_count"], 1)
                for result in case_results
            ),
            4,
        )
        if case_results
        else 0.0,
        "citation_coverage": round(citation_covered / total, 4) if total else 0.0,
        "tenant_leakage_count": tenant_leaks,
        "security_test_pass_rate": round(security_passes / len(security_cases), 4)
        if security_cases
        else 1.0,
        "avg_latency_ms": round(mean(result["latency_ms"] for result in case_results), 3)
        if case_results
        else 0.0,
        "total_cost_eur": round(sum(result["cost_eur"] for result in case_results), 6),
        "abstention_rate": round(sum(result["abstained"] for result in case_results) / total, 4)
        if total
        else 0.0,
        "duration_ms": round((perf_counter() - started) * 1000, 3),
        "cases": case_results,
    }
    (evidence_dir / "rag-evaluation.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    metrics.write_json(evidence_dir / "performance-report.json")
    (evidence_dir / "cost-report.json").write_text(
        json.dumps(
            {
                "status": "measured",
                "request_count": total,
                "total_tokens": metrics.total_tokens,
                "total_cost_eur": round(metrics.total_cost_eur, 6),
                "provider": "deterministic",
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return report

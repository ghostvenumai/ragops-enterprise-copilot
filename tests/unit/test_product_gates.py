from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from scripts import product_verify, review, verify


def test_product_timeout_is_not_a_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(product_verify, "OUT", tmp_path)

    def timeout(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired(["synthetic"], 1, output=b"unfinished check")

    monkeypatch.setattr(product_verify.subprocess, "run", timeout)
    result = product_verify.execute("synthetic-gate", ["synthetic"], timeout=1)
    assert result["status"] == "FAILED"
    assert "unfinished check" in (tmp_path / "synthetic-gate.log").read_text()


def test_required_migration_cannot_pass_when_dependencies_are_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ragops.evaluation import runner

    out = tmp_path / "evidence/product-v1"
    out.mkdir(parents=True)
    (tmp_path / "evidence/security-report.json").write_text('{"status": "passed"}')
    monkeypatch.setattr(product_verify, "ROOT", tmp_path)
    monkeypatch.setattr(product_verify, "OUT", out)
    monkeypatch.setattr(product_verify, "PENDING", {})
    monkeypatch.setattr(product_verify, "missing_modules", lambda names: ["sqlalchemy"])
    monkeypatch.setattr(
        product_verify,
        "execute",
        lambda name, command, **kw: {
            "name": name,
            "status": "PASSED",
        },
    )
    monkeypatch.setattr(runner, "run_evaluation", lambda *args: {"status": "passed"})
    monkeypatch.delenv("RAGOPS_TEST_DATABASE_URL", raising=False)
    # main changes only these process settings; restore them after this contract test.
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert product_verify.main() == 1
    report = json.loads((out / "release-gate.json").read_text())
    assert report["status"] == "BLOCKED"
    assert any(
        r["name"] == "migrations" and r["status"] == "NOT_EXECUTED" for r in report["results"]
    )


def test_review_checks_source_instead_of_emitting_unconditional_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "loop").mkdir()
    source = tmp_path / "loop/controller.py"
    monkeypatch.setattr(review, "REPO_ROOT", tmp_path)
    source.write_text('subprocess.run(["synthetic"], shell=True)')
    assert review.check_no_shell_execution() is False
    source.write_text('subprocess.run(["synthetic"], shell=False)')
    assert review.check_no_shell_execution() is True


def test_central_gate_timeout_overwrites_previous_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(verify, "EVIDENCE_DIR", tmp_path)
    monkeypatch.setattr(verify.shutil, "which", lambda name: name)
    target = tmp_path / "previous.json"
    target.write_text('{"status": "passed"}')

    def timeout(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired(["synthetic"], 120)

    monkeypatch.setattr(verify.subprocess, "run", timeout)
    result = verify.run_command("synthetic", ["synthetic"], "previous.json")
    assert result["status"] == "failed"
    assert json.loads(target.read_text())["status"] == "failed"


def test_unavailable_dependency_scan_writes_error_not_empty_or_stale_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(verify, "EVIDENCE_DIR", tmp_path)
    monkeypatch.setattr(verify.shutil, "which", lambda name: name)
    monkeypatch.setattr(
        verify.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            ["synthetic"], 1, "", "resolver unavailable"
        ),
    )
    verify.run_command("dependency-audit", ["synthetic"], "dependency-audit.json")
    report = json.loads((tmp_path / "dependency-audit.json").read_text())
    assert report["status"] == "failed"
    assert report["stderr_tail"] == "resolver unavailable"


def test_partial_runs_never_replace_the_full_verification_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(verify, "EVIDENCE_DIR", tmp_path)
    monkeypatch.setattr(verify, "REPO_ROOT", tmp_path)
    full = tmp_path / "verify-summary.json"
    full.write_text('{"status": "failed", "mode": "full"}')
    report = tmp_path / "security-report.json"
    report.write_text('{"status": "failed"}')
    monkeypatch.setattr(
        verify, "run_command", lambda name, *args, **kwargs: {"name": name, "status": "passed"}
    )
    bundles: list[str] = []
    monkeypatch.setattr("scripts.build_evidence_bundle.main", lambda: bundles.append("built"))

    assert verify.main(["--only", "lint"]) == 0
    assert json.loads(full.read_text()) == {"status": "failed", "mode": "full"}
    assert json.loads(report.read_text()) == {"status": "failed"}
    partial = json.loads((tmp_path / "verify-summary-lint.json").read_text())
    assert (partial["status"], partial["mode"]) == ("passed", "lint")
    assert bundles == []
    assert verify.summary_file(None) == "verify-summary.json"
    assert verify.summary_file("security") == "verify-summary-security.json"


def test_pytest_gets_its_own_hang_guard_and_other_gates_keep_120_seconds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(verify, "EVIDENCE_DIR", tmp_path)
    monkeypatch.setattr(verify.shutil, "which", lambda name: name)
    seen: list[object] = []

    def timeout(*args: object, **kwargs: object) -> None:
        seen.append(kwargs["timeout"])
        raise subprocess.TimeoutExpired(["synthetic"], kwargs["timeout"])  # type: ignore[arg-type]

    monkeypatch.setattr(verify.subprocess, "run", timeout)
    assert verify.run_pytest()["reason"] == "gate timed out after 600s"
    assert verify.run_command("ruff", ["synthetic"])["reason"] == "gate timed out after 120s"
    assert seen == [verify.PYTEST_TIMEOUT_SECONDS, 120] and verify.PYTEST_TIMEOUT_SECONDS == 600

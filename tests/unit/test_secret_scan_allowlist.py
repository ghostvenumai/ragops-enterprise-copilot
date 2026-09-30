"""The secret scan explains only exact documented synthetic literals, nothing nearby."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from scripts import verify

ROOT = Path(__file__).resolve().parents[2]
# Assembled at run time so this file never contains a scanner match itself.
HEADER = "Authorization" + ": " + "Bearer "
APPROVED = HEADER + "{key}"
ASSIGNED = "api" + "_key" + '="'


def scan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, files: dict[str, str], allow) -> dict:
    for name, text in files.items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    monkeypatch.setattr(verify, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(verify, "EVIDENCE_DIR", tmp_path / "evidence")
    monkeypatch.setattr(verify, "SECRET_SCAN_ALLOWLIST", tuple(allow))
    report = verify.secret_scan()
    assert (
        json.loads((tmp_path / "evidence/secret-scan.json").read_text())["status"]
        == (report["status"])
    )
    return report


def entry(path: str, pattern: int, literal: str) -> verify.SyntheticLiteral:
    digest = hashlib.sha256(literal.encode()).hexdigest()
    return verify.SyntheticLiteral(path, pattern, digest, "synthetic test literal")


def test_documented_literal_is_explained_with_its_rationale(tmp_path, monkeypatch) -> None:
    report = scan(
        tmp_path,
        monkeypatch,
        {"tests/a.py": f'message = f"{APPROVED}; rejected"\n'},
        [entry("tests/a.py", 1, APPROVED)],
    )
    assert report["status"] == "passed" and report["findings"] == []
    assert report["explained_synthetic_literals"] == [
        {
            "path": "tests/a.py",
            "pattern": verify.SECRET_PATTERNS[1].pattern,
            "rationale": "synthetic test literal",
        }
    ]


@pytest.mark.parametrize(
    "nearby",
    [
        HEADER + "{kez}",
        HEADER + "{key}x",
        HEADER + "abcdefghijklmnopqrstuvwxyz0124",
        HEADER.lower() + "{key}",
    ],
    # Plain ids keep the scanner-shaped values out of test reports.
    ids=["other-placeholder", "suffix", "other-value", "lower-case"],
)
def test_a_nearby_unapproved_value_in_the_same_file_is_still_a_finding(
    tmp_path, monkeypatch, nearby
) -> None:
    report = scan(
        tmp_path,
        monkeypatch,
        {"tests/a.py": f'first = "{APPROVED}; a"\nsecond = "{nearby}; b"\n'},
        [entry("tests/a.py", 1, APPROVED)],
    )
    assert report["status"] == "failed"
    assert [item["path"] for item in report["findings"]] == ["tests/a.py"]
    assert report["findings"][0]["preview"] == "[REDACTED]"
    assert len(report["explained_synthetic_literals"]) == 1


def test_the_same_literal_in_another_file_or_pattern_is_a_finding(tmp_path, monkeypatch) -> None:
    report = scan(
        tmp_path,
        monkeypatch,
        {"tests/a.py": f'"{APPROVED}; a"\n', "tests/b.py": f'"{APPROVED}; a"\n'},
        [entry("tests/a.py", 1, APPROVED)],
    )
    assert [item["path"] for item in report["findings"]] == ["tests/b.py"]
    wrong_pattern = scan(
        tmp_path,
        monkeypatch,
        {"tests/a.py": f'"{APPROVED}; a"\n'},
        [entry("tests/a.py", 2, APPROVED)],
    )
    assert wrong_pattern["status"] == "failed"
    assert {item["path"] for item in wrong_pattern["findings"]} == {"tests/a.py", "tests/b.py"}


def test_every_other_pattern_in_an_allowlisted_file_is_still_scanned(tmp_path, monkeypatch) -> None:
    report = scan(
        tmp_path,
        monkeypatch,
        {"tests/a.py": f'"{APPROVED}; a"\nclient({ASSIGNED}unapproved-value-123")\n'},
        [entry("tests/a.py", 1, APPROVED)],
    )
    assert report["status"] == "failed"
    assert report["findings"][0]["pattern"] == verify.SECRET_PATTERNS[2].pattern


def test_an_allowlist_entry_that_matches_nothing_fails_the_scan(tmp_path, monkeypatch) -> None:
    report = scan(
        tmp_path, monkeypatch, {"tests/a.py": "x = 1\n"}, [entry("tests/a.py", 1, APPROVED)]
    )
    assert report["status"] == "failed" and report["findings"] == []
    assert report["unused_allowlist_entries"] == [
        {"path": "tests/a.py", "rationale": "synthetic test literal"}
    ]


def test_repository_allowlist_is_exact_documented_and_limited_to_tests() -> None:
    keys = {(item.path, item.pattern, item.sha256) for item in verify.SECRET_SCAN_ALLOWLIST}
    assert len(keys) == len(verify.SECRET_SCAN_ALLOWLIST)
    for item in verify.SECRET_SCAN_ALLOWLIST:
        assert item.path.startswith("tests/") and item.path.endswith(".py")
        assert not any(character in item.path for character in "*?[")
        assert (ROOT / item.path).is_file()
        assert 0 <= item.pattern < len(verify.SECRET_PATTERNS)
        assert len(item.sha256) == 64 and int(item.sha256, 16) >= 0
        assert len(item.rationale) >= 20


def test_repository_secret_scan_has_no_unexplained_hit(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(verify, "EVIDENCE_DIR", tmp_path)
    report = verify.secret_scan()
    assert report["findings"] == [] and report["unused_allowlist_entries"] == []
    assert len(report["explained_synthetic_literals"]) == len(verify.SECRET_SCAN_ALLOWLIST)
    assert verify.check_no_env_files()["status"] == "passed"
    assert verify.check_no_tracked_secret_files()["status"] == "passed"


def test_integration_template_contains_only_placeholders() -> None:
    text = (ROOT / ".env.integration.example").read_text(encoding="utf-8")
    assert verify.env_template_issues(text) == []


@pytest.mark.parametrize(
    ("line", "name"),
    [
        ("KEYCLOAK_ADMIN_PASSWORD=synthetic-value", "KEYCLOAK_ADMIN_PASSWORD"),
        ("OPENAI_API_KEY=synthetic-value", "OPENAI_API_KEY"),
        ("RAGOPS_OIDC_PUBLIC_KEY=synthetic-value", "RAGOPS_OIDC_PUBLIC_KEY"),
        (
            "RAGOPS_DATABASE_URL=postgresql+psycopg://ragops:synthetic@db:5432/x",
            "RAGOPS_DATABASE_URL",
        ),
        ("RAGOPS_REDIS_URL=redis://:synthetic@redis:6379/0", "RAGOPS_REDIS_URL"),
        ("export", "export"),
    ],
)
def test_a_filled_in_template_value_is_reported_by_name_only(line, name) -> None:
    assert verify.env_template_issues(f"# comment\nRAGOPS_ENV=production\n{line}\n") == [name]


def test_only_known_root_templates_with_placeholder_content_are_allowed(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(verify, "REPO_ROOT", tmp_path)
    template = tmp_path / ".env.integration.example"
    template.write_text("KEYCLOAK_ADMIN_PASSWORD=\nRAGOPS_REDIS_URL=redis://redis:6379/0\n")
    (tmp_path / ".env.example").write_text("RAGOPS_ENV=local\n")
    assert verify.check_no_env_files()["status"] == "passed"
    template.write_text("KEYCLOAK_ADMIN_PASSWORD=synthetic-value\n")
    assert verify.check_no_env_files()["unexpected_env_files"] == [".env.integration.example"]
    template.write_text("KEYCLOAK_ADMIN_PASSWORD=\n")
    for name in (".env", ".env.local", ".env.integration", "deploy/.env.integration.example"):
        other = tmp_path / name
        other.parent.mkdir(parents=True, exist_ok=True)
        other.write_text("RAGOPS_ENV=local\n")
        assert verify.check_no_env_files()["unexpected_env_files"] == [name]
        assert not verify.env_template_is_secret_free(name)
        other.unlink()

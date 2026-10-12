"""Operator CLI for reservation reconciliation: JSON only, tenant-scoped, no override switch."""

from __future__ import annotations

import json

import pytest
from scripts import reconcile_reservations as cli
from tests.security.finops_helpers import migrated_sqlite
from tests.security.reconciliation_helpers import QUESTION, Scenario


@pytest.fixture
def world(tmp_path, monkeypatch):
    engine, url = migrated_sqlite(tmp_path / "cli.db")
    monkeypatch.setenv("RAGOPS_DATABASE_URL", url)
    return Scenario(engine), url


def run(capsys, *argv: str) -> tuple[int, dict]:
    code = cli.main(["--grace-seconds", "0", *argv])
    out = capsys.readouterr().out
    return code, json.loads(out)


def test_list_inspect_reconcile_and_auto(world, capsys) -> None:
    scenario, url = world
    held = scenario.provider_timeout(key="k-held")
    receipt = scenario.receipt_then_crash(key="k-receipt")
    pre = scenario.crash_before_boundary(key="k-pre")
    code, listing = run(capsys, "--tenant", "tenant-a", "list")
    assert code == 0 and listing["count"] == 3
    assert listing["auto_safe"] == 2 and listing["review_required"] == 1
    code, inspected = run(
        capsys, "--tenant", "tenant-a", "inspect", str(held.ticket.correlation_id)
    )
    assert inspected["case"]["classification"] == "unknown_billing"
    assert inspected["case"]["ambiguity_reason"] == "provider_timeout"
    code, refused = run(
        capsys, "--tenant", "tenant-a", "reconcile", str(held.ticket.reservation_id)
    )
    assert code == 0 and refused["outcome"]["action"] == "none"
    assert refused["outcome"]["review_required"] is True
    code, batch = run(capsys, "--tenant", "tenant-a", "auto", "--limit", "10")
    assert batch["changed"] == 2
    assert {item["reservation_id"]: item["action"] for item in batch["outcomes"]} == {
        str(receipt.ticket.reservation_id): "committed",
        str(pre.ticket.reservation_id): "released",
    }
    everything = json.dumps([listing, inspected, refused, batch])
    for value in (url, QUESTION, "k-held", "k-receipt", "k-pre"):
        assert value not in everything


def test_another_tenant_gets_not_found(world, capsys) -> None:
    scenario, _ = world
    started = scenario.receipt_then_crash()
    scenario.tenant("tenant-b")
    code, result = run(
        capsys, "--tenant", "tenant-b", "reconcile", str(started.ticket.reservation_id)
    )
    assert code == cli.EXIT_NOT_FOUND and result == {
        "tenant": "tenant-b",
        "error": "case not found",
    }
    assert scenario.snapshot()["usage"] == []


def test_there_is_no_override_switch() -> None:
    options = {option for action in cli.parser()._actions for option in action.option_strings}
    for sub in cli.parser()._subparsers._group_actions[0].choices.values():
        options |= {option for action in sub._actions for option in action.option_strings}
    for forbidden in ("force", "trust", "assume", "release", "not-billed", "override"):
        assert not any(forbidden in option for option in options)


def test_arguments_are_bounded_and_the_url_is_required(monkeypatch, capsys) -> None:
    with pytest.raises(SystemExit):
        cli.main(["--tenant", "t", "--grace-seconds", "-1", "list"])
    with pytest.raises(SystemExit):
        cli.main(["--tenant", "t", "inspect", "not-a-uuid"])
    capsys.readouterr()
    monkeypatch.delenv("RAGOPS_DATABASE_URL", raising=False)
    assert cli.main(["--tenant", "t", "list"]) == cli.EXIT_USAGE


def test_an_unavailable_datastore_is_reported_without_details(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("RAGOPS_DATABASE_URL", f"sqlite:///{tmp_path / 'missing' / 'x.db'}")
    code = cli.main(["--tenant", "t", "list"])
    assert code == cli.EXIT_UNAVAILABLE
    assert json.loads(capsys.readouterr().out) == {
        "tenant": "t",
        "error": "reconciliation unavailable",
    }

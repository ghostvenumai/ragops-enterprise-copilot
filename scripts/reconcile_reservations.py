"""Operator CLI for query reservation reconciliation (ENT-11.6).

The database URL comes from RAGOPS_DATABASE_URL, never from arguments, so it does not reach
shell history. Every command works inside exactly one explicit tenant and prints a non-secret
JSON document (identifiers, states, counts and amounts only).

  list       open cases of the tenant with their classification
  inspect    one case by reservation, ledger or correlation id
  reconcile  the one safe action for one case, or the reason why there is none
  auto       bounded batch of cases that durable evidence makes safe

Reconciliation acts only on durable internal evidence and never calls a provider. A case
whose provider may have billed without a durable receipt stays held: there is deliberately
no option to force a release or to assume that nothing was billed. Provider-native billing
evidence would be a later extension.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine  # noqa: E402

from ragops.finops.reconciliation import (  # noqa: E402
    MAX_BATCH,
    MAX_GRACE_SECONDS,
    CaseNotFound,
    ReconciliationService,
    ReconciliationUnavailable,
)
from ragops.workflows.query_idempotency import DEFAULT_LEASE_SECONDS  # noqa: E402

EXIT_OK, EXIT_UNAVAILABLE, EXIT_NOT_FOUND, EXIT_USAGE = 0, 3, 4, 2


def _bounded(low: int, high: int) -> Any:
    def parse(value: str) -> int:
        number = int(value)
        if not low <= number <= high:
            raise argparse.ArgumentTypeError(f"must be between {low} and {high}")
        return number

    return parse


def _identifier(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a UUID") from None


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    root.add_argument("--tenant", required=True, help="tenant whose cases are handled")
    root.add_argument(
        "--grace-seconds",
        type=_bounded(0, MAX_GRACE_SECONDS),
        default=DEFAULT_LEASE_SECONDS,
        help="leave cases younger than this alone (liveness only, never billing evidence)",
    )
    commands = root.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list")
    listing.add_argument("--limit", type=_bounded(1, MAX_BATCH), default=100)
    for name in ("inspect", "reconcile"):
        command = commands.add_parser(name)
        command.add_argument("id", type=_identifier, help="reservation, ledger or correlation id")
    auto = commands.add_parser("auto")
    auto.add_argument("--limit", type=_bounded(1, MAX_BATCH), default=50)
    return root


def run(args: argparse.Namespace, service: ReconciliationService) -> dict[str, Any]:
    if args.command == "list":
        cases = service.cases(args.limit)
        return {
            "tenant": args.tenant,
            "cases": [case.as_dict() for case in cases],
            "count": len(cases),
            "auto_safe": sum(case.auto_safe for case in cases),
            "review_required": sum(case.review_required for case in cases),
        }
    if args.command == "inspect":
        return {"tenant": args.tenant, "case": service.inspect(args.id).as_dict()}
    if args.command == "reconcile":
        return {"tenant": args.tenant, "outcome": service.reconcile(args.id).as_dict()}
    outcomes = service.reconcile_auto(args.limit)
    return {
        "tenant": args.tenant,
        "outcomes": [outcome.as_dict() for outcome in outcomes],
        "changed": sum(outcome.changed for outcome in outcomes),
    }


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    url = os.getenv("RAGOPS_DATABASE_URL")
    if not url:
        print(json.dumps({"error": "RAGOPS_DATABASE_URL is required"}))
        return EXIT_USAGE
    engine = create_engine(url, hide_parameters=True)
    try:
        service = ReconciliationService(
            lambda: engine, args.tenant, grace_seconds=args.grace_seconds
        )
        print(json.dumps(run(args, service), indent=2, sort_keys=True))
        return EXIT_OK
    except CaseNotFound:
        print(json.dumps({"tenant": args.tenant, "error": "case not found"}))
        return EXIT_NOT_FOUND
    except ReconciliationUnavailable:
        print(json.dumps({"tenant": args.tenant, "error": "reconciliation unavailable"}))
        return EXIT_UNAVAILABLE
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}))
        return EXIT_USAGE
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())

"""Operator CLI for privacy egress policies (ENT-13.1); the only write path for policies.

Uses RAGOPS_DATABASE_URL / RAGOPS_DATABASE_URL_FILE like every other tool. Every change
records the operator (``--actor``) and a reason in an append-only revision. Output is JSON
metadata only. A policy is a technical authorization; it does not establish a legal basis
or consent (see docs/adr/0021-privacy-egress-gate.md).

Examples:
  privacy_egress_admin.py profile --tenant t --synthetic-only --actor ops@example
  privacy_egress_admin.py grant --tenant t --provider openai --model gpt-x \\
      --data-classes user_question,document_text --scope synthetic_test_data \\
      --valid-days 7 --actor ops@example --reason "synthetic evaluation"
  privacy_egress_admin.py revoke --tenant t --policy-id <uuid> --expected-version 1 \\
      --actor ops@example --reason "evaluation ended"
  privacy_egress_admin.py list --tenant t
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from ragops.persistence.database import open_engine  # noqa: E402
from ragops.privacy import policies  # noqa: E402

MAX_VALID_DAYS = 366


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = root.add_subparsers(dest="command", required=True)
    profile = commands.add_parser(
        "profile", help="state whether a tenant holds synthetic data only"
    )
    profile.add_argument("--tenant", required=True)
    group = profile.add_mutually_exclusive_group(required=True)
    group.add_argument("--synthetic-only", action="store_true")
    group.add_argument("--personal-data", action="store_true")
    profile.add_argument("--actor", required=True)
    grant = commands.add_parser("grant", help="create an exact, expiring authorization")
    grant.add_argument("--tenant", required=True)
    grant.add_argument("--purpose", default="rag_answer")
    grant.add_argument("--provider", required=True)
    grant.add_argument("--model", required=True)
    grant.add_argument("--data-classes", required=True, help="comma separated")
    grant.add_argument("--scope", required=True, choices=["synthetic_test_data", "personal_data"])
    grant.add_argument("--valid-days", type=int, required=True)
    grant.add_argument("--legal-basis-ref")
    grant.add_argument("--processor-agreement-ref")
    grant.add_argument("--transfer-mechanism-ref")
    grant.add_argument("--retention-control", choices=["store_false", "zero_data_retention"])
    grant.add_argument("--actor", required=True)
    grant.add_argument("--reason", required=True)
    for action in policies.TRANSITIONS:
        change = commands.add_parser(action, help=f"{action} a policy (optimistic concurrency)")
        change.add_argument("--tenant", required=True)
        change.add_argument("--policy-id", required=True, type=UUID)
        change.add_argument("--expected-version", required=True, type=int)
        change.add_argument("--actor", required=True)
        change.add_argument("--reason", required=True)
    listing = commands.add_parser("list", help="show a tenant's policies")
    listing.add_argument("--tenant", required=True)
    return root


def run(arguments: list[str]) -> dict[str, object]:
    args = parser().parse_args(arguments)
    engine = open_engine()
    try:
        if args.command == "profile":
            policies.record_tenant_profile(
                engine, args.tenant, synthetic_data_only=args.synthetic_only, actor=args.actor
            )
            return {"tenant": args.tenant, "synthetic_data_only": bool(args.synthetic_only)}
        if args.command == "grant":
            if not 1 <= args.valid_days <= MAX_VALID_DAYS:
                raise policies.PolicyError(f"--valid-days must be 1-{MAX_VALID_DAYS}")
            now = datetime.now(UTC)
            policy = policies.grant(
                engine,
                policies.PolicyGrant(
                    tenant_id=args.tenant,
                    purpose=args.purpose,
                    provider_id=args.provider,
                    model_id=args.model,
                    data_classes=tuple(
                        c.strip() for c in args.data_classes.split(",") if c.strip()
                    ),
                    data_scope=args.scope,
                    valid_from=now,
                    valid_until=now + timedelta(days=args.valid_days),
                    actor=args.actor,
                    reason=args.reason,
                    legal_basis_ref=args.legal_basis_ref,
                    processor_agreement_ref=args.processor_agreement_ref,
                    transfer_mechanism_ref=args.transfer_mechanism_ref,
                    retention_control=args.retention_control,
                ),
            )
            return policies.policy_view(policy)
        if args.command in policies.TRANSITIONS:
            version = policies.change_state(
                engine,
                args.tenant,
                args.policy_id,
                args.command,
                expected_version=args.expected_version,
                actor=args.actor,
                reason=args.reason,
            )
            return {"policy_id": str(args.policy_id), "version": version, "action": args.command}
        return {"tenant": args.tenant, "policies": policies.list_policies(engine, args.tenant)}
    finally:
        engine.dispose()


def main(argv: list[str] | None = None) -> int:
    try:
        result = run(sys.argv[1:] if argv is None else argv)
    except (policies.PolicyError, policies.PolicyConflict, ValueError) as exc:
        print(json.dumps({"status": "REJECTED", "reason": str(exc)}))
        return 2
    print(json.dumps({"status": "OK", **result}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import json
from pathlib import Path

from ragops.config.settings import Settings


def main() -> int:
    settings = Settings.from_env()
    checks: dict[str, str] = {}
    try:
        settings.validate_identity_configuration()
        settings.validate_async_configuration()
        settings.validate_vector_configuration()
        settings.validate_model_configuration()
        checks["configuration"] = "PASS"
    except (RuntimeError, ValueError) as exc:
        checks["configuration"] = f"FAIL: {exc}"
    try:
        if settings.environment == "production":
            settings.require_supported_runtime()
    except RuntimeError as exc:
        checks["runtime"] = f"BLOCKED: {exc}"
    output = Path(settings.evidence_dir) / "product-v1" / "production-config-validation.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(checks, indent=2), encoding="utf-8")
    return 0 if checks.get("configuration") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

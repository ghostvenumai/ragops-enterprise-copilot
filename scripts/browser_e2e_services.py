"""Start the API or the worker for the browser gate behind a provider watchdog.

Usage: browser_e2e_services.py api <port> | worker

The watchdog makes constructing a paid provider fail and records every provider event as one
line (``paid`` or ``invoke``) in the gate-owned ledger named by RAGOPS_E2E_PROVIDER_LEDGER, so
the gate can prove paid_provider_calls = 0 for processes it does not run in-process.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def install_provider_watchdog(ledger: Path) -> None:
    from ragops.llm import providers

    def record(event: str) -> None:
        with ledger.open("a", encoding="utf-8") as handle:
            handle.write(event + "\n")

    def paid(self: Any, *args: Any, **kwargs: Any) -> None:
        record("paid")
        raise RuntimeError("paid providers are disabled in the browser gate")

    original_generate = providers.DeterministicTestProvider.generate

    def generate(self: Any, *args: Any, **kwargs: Any) -> Any:
        record("invoke")
        return original_generate(self, *args, **kwargs)

    providers.OpenAIProvider.__init__ = paid  # type: ignore[method-assign]
    providers.AzureOpenAIProvider.__init__ = paid  # type: ignore[method-assign]
    providers.DeterministicTestProvider.generate = generate  # type: ignore[method-assign]


def main() -> int:
    install_provider_watchdog(Path(os.environ["RAGOPS_E2E_PROVIDER_LEDGER"]))
    if sys.argv[1:2] == ["api"]:
        import uvicorn

        uvicorn.run(
            "apps.api.main:app", host="127.0.0.1", port=int(sys.argv[2]), log_level="warning"
        )
        return 0
    if sys.argv[1:2] == ["worker"]:
        from ragops.workers.runtime import main as worker_main

        worker_main()
        return 0
    raise SystemExit("usage: browser_e2e_services.py api <port> | worker")


if __name__ == "__main__":
    raise SystemExit(main())

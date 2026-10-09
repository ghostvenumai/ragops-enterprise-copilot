"""Start the API or the worker for the browser gate behind a provider watchdog.

Usage: browser_e2e_services.py api <port> | worker

With RAGOPS_E2E_REQUEST_LOG set, the API records request metadata for failure diagnostics.

The watchdog makes constructing a paid provider fail and records every provider event as one
line (``paid`` or ``invoke``) in the gate-owned ledger named by RAGOPS_E2E_PROVIDER_LEDGER, so
the gate can prove paid_provider_calls = 0 for processes it does not run in-process.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

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


class RequestRecorder:
    """ASGI wrapper that appends request metadata to a gate-owned JSONL file.

    Each request writes a ``start`` line when it arrives and an ``end`` line with the status
    when it finishes, so a request still in flight is visible too. Only an id, method, path
    (never the query string), status and wall-clock time are written: no headers, cookies,
    tokens or bodies. The browser gate correlates these lines with browser events and
    database rows when a journey fails.
    """

    def __init__(self, app: Any, log: Path) -> None:
        self.app, self.log = app, log

    def _write(self, record: dict[str, Any]) -> None:
        with self.log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        request_id = uuid4().hex[:12]
        status: list[int] = []
        self._write(
            {
                "id": request_id,
                "phase": "start",
                "method": scope.get("method"),
                "path": scope.get("path"),
                "t": time.time(),
            }
        )

        async def tracking_send(message: Any) -> None:
            if message.get("type") == "http.response.start":
                status.append(int(message.get("status", 0)))
            await send(message)

        try:
            await self.app(scope, receive, tracking_send)
        finally:
            self._write(
                {
                    "id": request_id,
                    "phase": "end",
                    "status": status[0] if status else None,
                    "t": time.time(),
                }
            )


def main() -> int:
    install_provider_watchdog(Path(os.environ["RAGOPS_E2E_PROVIDER_LEDGER"]))
    if sys.argv[1:2] == ["api"]:
        import uvicorn
        from apps.api.main import app

        request_log = os.environ.get("RAGOPS_E2E_REQUEST_LOG")
        served = RequestRecorder(app, Path(request_log)) if request_log else app
        uvicorn.run(served, host="127.0.0.1", port=int(sys.argv[2]), log_level="warning")
        return 0
    if sys.argv[1:2] == ["worker"]:
        from ragops.workers.runtime import main as worker_main

        worker_main()
        return 0
    raise SystemExit("usage: browser_e2e_services.py api <port> | worker")


if __name__ == "__main__":
    raise SystemExit(main())

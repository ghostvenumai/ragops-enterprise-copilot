"""Loopback TCP proxy that injects dependency failures for one gate-owned client.

forward  relays bytes to the real service.
refuse   closes the listener, so new connections are refused (ECONNREFUSED).
blackhole accepts connections but never answers, so clients hit their timeouts.
cut()    closes every active relayed connection, like a mid-operation disconnect.

``upstream_bytes`` counts the bytes relayed from clients to the upstream service, so a gate
can observe that real requests reached it.

Only the client configured with this proxy's port is affected; the upstream service and
its other clients are never stopped, paused or reconfigured. The listener keeps its port
across refuse/forward, so a recovered client reconnects to the same address.
"""

from __future__ import annotations

import socket
import threading
from contextlib import suppress


class FaultProxy:
    def __init__(self, upstream_host: str, upstream_port: int) -> None:
        self.upstream = (upstream_host, upstream_port)
        self.mode = "forward"
        self._lock = threading.Lock()
        self._sockets: set[socket.socket] = set()
        self._stopped = threading.Event()
        self._listener: socket.socket | None = None
        self.port = 0
        self.upstream_bytes = 0
        self._open_listener()
        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()

    @property
    def address(self) -> tuple[str, int]:
        return "127.0.0.1", self.port

    def _open_listener(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", self.port))
        listener.listen(64)
        listener.settimeout(0.1)
        self.port = listener.getsockname()[1]
        self._listener = listener

    def _close_listener(self) -> None:
        # shutdown() stops listening at once; a bare close() keeps accepting until the
        # accept() blocked in the other thread returns.
        if self._listener is None:
            return
        with suppress(OSError):
            self._listener.shutdown(socket.SHUT_RDWR)
        self._listener.close()
        self._listener = None

    def set_mode(self, mode: str) -> None:
        if mode not in {"forward", "refuse", "blackhole"}:
            raise ValueError("mode must be forward, refuse or blackhole")
        with self._lock:
            self.mode = mode
            if mode == "refuse" and self._listener is not None:
                self._close_listener()
            elif mode != "refuse" and self._listener is None:
                self._open_listener()
        if mode != "forward":
            self.cut()

    def cut(self) -> None:
        with self._lock:
            sockets, self._sockets = self._sockets, set()
        for connection in sockets:
            with suppress(OSError):
                connection.shutdown(socket.SHUT_RDWR)
            connection.close()

    def stop(self) -> None:
        self._stopped.set()
        with self._lock:
            if self._listener is not None:
                self._close_listener()
        self.cut()
        self._thread.join(timeout=2)

    def _accept_loop(self) -> None:
        while not self._stopped.is_set():
            with self._lock:
                listener = self._listener
            if listener is None:
                self._stopped.wait(0.05)
                continue
            try:
                client, _ = listener.accept()
            except (TimeoutError, OSError):
                continue
            with self._lock:
                self._sockets.add(client)
                mode = self.mode
            if mode == "blackhole":
                continue  # held open, never answered
            try:
                upstream = socket.create_connection(self.upstream, timeout=2)
            except OSError:
                client.close()
                continue
            with self._lock:
                self._sockets.add(upstream)
            for source, target, to_upstream in (
                (client, upstream, True),
                (upstream, client, False),
            ):
                threading.Thread(
                    target=self._pump, args=(source, target, to_upstream), daemon=True
                ).start()

    def _pump(
        self, source: socket.socket, target: socket.socket, to_upstream: bool = False
    ) -> None:
        try:
            while data := source.recv(65536):
                target.sendall(data)
                if to_upstream:
                    with self._lock:
                        self.upstream_bytes += len(data)
        except OSError:
            pass
        finally:
            for connection in (source, target):
                with suppress(OSError):
                    connection.shutdown(socket.SHUT_RDWR)
                connection.close()

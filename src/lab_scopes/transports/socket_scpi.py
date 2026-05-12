"""Small newline-terminated SCPI-over-TCP transport."""

from __future__ import annotations

import socket

from lab_scopes.errors import ScopeConnectionError, ScopeTimeoutError


class SocketScpiTransport:
    """Plain TCP transport for instruments that speak newline SCPI."""

    def __init__(self, host: str, port: int, timeout: float = 5.0, terminator: bytes = b"\n"):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.terminator = terminator
        self.chunk_size = 1024 * 1024
        self._sock: socket.socket | None = None

    def open(self) -> None:
        try:
            self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
            self._sock.settimeout(self.timeout)
        except OSError as exc:
            raise ScopeConnectionError(f"cannot connect to {self.host}:{self.port}: {exc}") from exc

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    @property
    def connected(self) -> bool:
        return self._sock is not None

    def write(self, command: str) -> None:
        self._require_socket().sendall(command.encode("ascii") + self.terminator)

    def read(self) -> str:
        data = bytearray()
        sock = self._require_socket()
        try:
            while not data.endswith(self.terminator):
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data.extend(chunk)
        except socket.timeout as exc:
            raise ScopeTimeoutError(f"timed out reading from {self.host}:{self.port}") from exc
        return bytes(data).decode("utf-8", errors="replace").strip()

    def query(self, command: str) -> str:
        self.write(command)
        return self.read()

    def read_raw(self, max_bytes: int | None = None) -> bytes:
        sock = self._require_socket()
        chunks: list[bytes] = []
        total = 0
        try:
            while max_bytes is None or total < max_bytes:
                request = self.chunk_size if max_bytes is None else min(self.chunk_size, max_bytes - total)
                chunk = sock.recv(request)
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if len(chunk) < request:
                    break
        except socket.timeout as exc:
            raise ScopeTimeoutError(f"timed out reading raw data from {self.host}:{self.port}") from exc
        return b"".join(chunks)

    def _require_socket(self) -> socket.socket:
        if self._sock is None:
            raise ScopeConnectionError("transport is not open")
        return self._sock

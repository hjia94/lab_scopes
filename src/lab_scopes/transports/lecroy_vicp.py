"""Native LeCroy VICP framing over TCP.

This module intentionally has no PyVISA dependency. The encode/decode helpers are
unit-tested without opening a socket; live communication uses port 1861.
"""

from __future__ import annotations

import socket
import struct

from lab_scopes.errors import ScopeConnectionError, ScopeProtocolError, ScopeTimeoutError

VICP_PORT = 1861
VICP_VERSION = 1

OP_DATA = 0x80
OP_REMOTE = 0x08
OP_LOCKOUT = 0x04
OP_CLEAR = 0x02
OP_EOI = 0x01

VICP_FRAME_HEADER_SIZE = 8


def encode_vicp_message(payload: bytes | str, *, sequence: int = 0, eoi: bool = True) -> bytes:
    """Return one VICP frame for `payload`."""
    if isinstance(payload, str):
        payload = payload.encode("ascii")
    operation = OP_DATA | OP_REMOTE | (OP_EOI if eoi else 0)
    frame_header = struct.pack(">BBBBL", operation, VICP_VERSION, sequence & 0xFF, 0, len(payload))
    return frame_header + payload


def decode_vicp_frame_header(frame_header: bytes) -> dict[str, int | bool]:
    """Decode an 8-byte VICP frame header."""
    if len(frame_header) != VICP_FRAME_HEADER_SIZE:
        raise ScopeProtocolError(f"VICP frame header must be 8 bytes, got {len(frame_header)}")
    operation, version, sequence, spare, payload_len = struct.unpack(">BBBBL", frame_header)
    return {
        "operation": operation,
        "version": version,
        "sequence": sequence,
        "spare": spare,
        "payload_len": payload_len,
        "eoi": bool(operation & OP_EOI),
        "data": bool(operation & OP_DATA),
    }


class LeCroyVICPTransport:
    """VICP-over-TCP transport with a PyVISA-like minimal surface."""

    def __init__(self, host: str, port: int = VICP_PORT, timeout: float = 5.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.chunk_size = 1024 * 1024
        self._sequence = 0
        self._sock: socket.socket | None = None

    def open(self) -> None:
        try:
            self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
            self._sock.settimeout(self.timeout)
        except OSError as exc:
            raise ScopeConnectionError(f"cannot connect to LeCroy scope at {self.host}:{self.port}: {exc}") from exc

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
        if not command.endswith("\n"):
            command += "\n"
        self._send(command.encode("ascii"))

    def read(self) -> str:
        return self.read_raw().decode("utf-8", errors="replace").strip()

    def query(self, command: str) -> str:
        self.write(command)
        return self.read()

    def read_raw(self) -> bytes:
        payloads: list[bytearray] = []
        total_size = 0
        while True:
            frame_header = self._recv_exact(VICP_FRAME_HEADER_SIZE)
            decoded = decode_vicp_frame_header(frame_header)
            chunk = self._recv_exact_into(int(decoded["payload_len"]))
            payloads.append(chunk)
            total_size += len(chunk)
            if decoded["eoi"]:
                break
        if len(payloads) == 1:
            return bytes(payloads[0])
        payload = bytearray(total_size)
        offset = 0
        for chunk in payloads:
            payload[offset:offset + len(chunk)] = chunk
            offset += len(chunk)
        return bytes(payload)

    def _send(self, payload: bytes) -> None:
        sock = self._require_socket()
        frame = encode_vicp_message(payload, sequence=self._sequence)
        self._sequence = (self._sequence + 1) & 0xFF
        try:
            sock.sendall(frame)
        except socket.timeout as exc:
            raise ScopeTimeoutError(f"timed out writing to {self.host}:{self.port}") from exc

    def _recv_exact(self, size: int) -> bytes:
        return bytes(self._recv_exact_into(size))

    def _recv_exact_into(self, size: int) -> bytearray:
        sock = self._require_socket()
        data = bytearray(size)
        view = memoryview(data)
        offset = 0
        try:
            while offset < size:
                received = sock.recv_into(view[offset:], size - offset)
                if not received:
                    raise ScopeConnectionError("connection closed while reading VICP data")
                offset += received
        except socket.timeout as exc:
            raise ScopeTimeoutError(f"timed out reading from {self.host}:{self.port}") from exc
        return data

    def _require_socket(self) -> socket.socket:
        if self._sock is None:
            raise ScopeConnectionError("transport is not open")
        return self._sock

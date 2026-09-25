# -*- coding: utf-8 -*-
"""Low-level SCPI transport for Rigol DHO800/DHO900 oscilloscopes.

Communicates via a raw TCP socket on LXI port 5555 (wrapped in telnetlib).
Binary waveform reads bypass telnetlib's IAC-byte processing, which would
silently corrupt any 0xFF sample bytes.

Public API:
    command()              Send a query or command; returns str or raw bytes.
    tmc_header_bytes()     IEEE 488.2 TMC block header length.
    expected_data_bytes()  Data payload length declared in the TMC header.
    expected_buff_bytes()  Total expected bytes: header + data + terminator.
    get_memory_depth()     Read :ACQuire:MDEPth? (raises on bad reply).

Every wait is capped by its own timeout and, when a ``Deadline`` is passed, by the time
left. After any raise the connection must not be reused: it may hold a late reply.
"""

import select
import socket
import time

from lab_scopes.errors import ScopeConnectionError, ScopeProtocolError, ScopeTimeoutError
from lab_scopes.transports.deadline import clamp, sleep


def _raw_socket_recv(tn, max_bytes, poll_timeout):
    """Read bytes directly from the underlying socket, bypassing telnetlib IAC processing.

    telnetlib interprets 0xFF as an IAC escape and silently drops the following
    byte. LXI port 5555 is a plain TCP socket (not a telnet server), so this
    behaviour corrupts waveform samples that happen to equal 0xFF.
    """
    sock = tn.sock
    ready, _, _ = select.select([sock], [], [], poll_timeout)
    if not ready:
        return b''
    chunk = sock.recv(max_bytes)
    if not chunk:
        # Readable with no data is EOF, not a pause.
        raise ScopeConnectionError("connection closed by the scope")
    return chunk


def _send(tn, scpi, deadline):
    step = tn.timeout  # the connection's send timeout (RigolDHO800 sets it after connecting)
    send_timeout = clamp(deadline, step)  # raises before sending when no time is left
    shortened = send_timeout < step
    if shortened:
        tn.sock.settimeout(send_timeout)
    try:
        tn.write((scpi + "\n").encode("utf-8"))
    except socket.timeout as exc:
        raise ScopeTimeoutError(f"timed out sending {scpi!r}") from exc
    except OSError as exc:
        raise ScopeConnectionError(f"failed to send {scpi!r}: {exc}") from exc
    finally:
        if shortened:
            tn.sock.settimeout(step)


def command(tn, scpi, timeout=15, binary_data=False, deadline=None):
    """Send a SCPI command or query and return the response.

    Returns a decoded string for text queries, or raw bytes for binary ones.
    Pass binary_data=True to force binary mode regardless of the SCPI string.
    """
    if scpi.endswith('?'):
        _send(tn, scpi, deadline)

        scpi_upper = scpi.upper()
        if binary_data or ':WAVEFORM:DATA?' in scpi_upper or ':DISPLAY:DATA?' in scpi_upper:
            # Bypass telnetlib IAC processing for binary data (see _raw_socket_recv).
            # Flush any bytes telnetlib may have buffered from prior text reads.
            tn.rawq = b''
            tn.irawq = 0
            tn.cookedq = b''
            response = bytearray()  # += on bytes would copy the whole block per chunk
            start_time = time.monotonic()
            # Allow up to max_idle_time of silence before the TMC header arrives.
            # Once the header is parsed we trust the global timeout instead, because
            # DHO firmware can pause >2 s before sending the trailing newline on
            # large RAW reads.
            max_idle_time = min(2.0, max(0.5, timeout / 4.0))
            last_data_time = start_time
            total_expected = None

            try:
                while time.monotonic() - start_time < timeout:
                    chunk = _raw_socket_recv(tn, 65536, poll_timeout=clamp(deadline, 0.05))
                    if chunk:
                        response += chunk
                        last_data_time = time.monotonic()

                        if total_expected is None:
                            if not response.startswith(b'#'):
                                marker_index = response.find(b'#')
                                if marker_index >= 0:
                                    del response[:marker_index]

                            if response.startswith(b'#') and len(response) >= 2:
                                header_length = tmc_header_bytes(bytes(response[:2]))
                                if len(response) >= header_length:
                                    total_expected = expected_buff_bytes(bytes(response[:header_length]))

                        if total_expected is not None and len(response) >= total_expected:
                            del response[total_expected:]
                            break
                    else:
                        if total_expected is None and time.monotonic() - last_data_time > max_idle_time:
                            break
                        time.sleep(0.01)
            except ValueError as exc:
                raise ScopeProtocolError(f"malformed TMC header in reply to {scpi!r}: {exc}") from exc
            except socket.timeout as exc:
                raise ScopeTimeoutError(f"timed out reading the reply to {scpi!r}") from exc
            except OSError as exc:
                raise ScopeConnectionError(f"failed reading the reply to {scpi!r}: {exc}") from exc

            response = bytes(response)
            if not response.startswith(b'#'):
                raise ScopeTimeoutError(f"No TMC header received for {scpi!r}")

            header_length = tmc_header_bytes(response)  # validated in the loop
            data_length = expected_data_bytes(response)
            total_expected = header_length + data_length + 1

            if len(response) < header_length:
                raise ScopeTimeoutError(f"Incomplete TMC header for {scpi!r}")

            if len(response) < total_expected:
                raise ScopeTimeoutError(
                    f"Incomplete binary block for {scpi!r}: got {len(response)}/{total_expected} bytes"
                )

            terminator = response[header_length + data_length:total_expected]
            if terminator not in (b'\n', b'\r'):
                raise ScopeProtocolError(
                    f"Invalid binary block terminator for {scpi!r}: {terminator!r}"
                )

            return response[:total_expected]

        else:
            try:
                response = tn.read_until(b"\n", clamp(deadline, timeout))
            except EOFError as exc:
                raise ScopeConnectionError(f"connection closed before the reply to {scpi!r}") from exc
            except OSError as exc:
                raise ScopeConnectionError(f"failed reading the reply to {scpi!r}: {exc}") from exc
            # read_until returns whatever arrived when it gives up; only a full line is a reply.
            if not response.endswith(b"\n"):
                if tn.eof:
                    raise ScopeConnectionError(f"connection closed mid-reply to {scpi!r} (got {response!r})")
                raise ScopeTimeoutError(f"no complete reply to {scpi!r} (got {response!r})")
            return response.decode("utf-8", errors='ignore').strip()

    else:
        _send(tn, scpi, deadline)
        # Short settling delay for commands that change scope state.
        if any(cmd in scpi.upper() for cmd in (
            ':TRIGGER:', ':ACQUIRE:', ':CHANNEL:', ':TIMEBASE:',
            ':WAVEFORM:SOURCE', ':WAVEFORM:MODE', ':WAVEFORM:FORMAT',
        )):
            sleep(deadline, 0.05)
        return ""


def tmc_header_bytes(buff):
    """Return the byte length of the IEEE 488.2 TMC definite-length block header.

    Format is ``#<N><Length><Data>`` where N is a single digit giving the number
    of digits in Length. Rigol DHO800/900 always emits ``#9...`` for waveform data.
    Raises ValueError for invalid or indefinite-length (#0) headers.
    """
    if isinstance(buff, bytes):
        if len(buff) < 2:
            return 0
        n_char = chr(buff[1])
    else:
        if len(buff) < 2:
            return 0
        n_char = buff[1]

    if not n_char.isdigit():
        raise ValueError(f"Invalid TMC header digit: {n_char!r}")
    n_digits = int(n_char)
    if n_digits == 0:
        raise ValueError("TMC indefinite-length block (#0) not supported here")
    return 2 + n_digits


def expected_data_bytes(buff):
    """Return the data payload length declared in the TMC header."""
    try:
        header_len = tmc_header_bytes(buff)
        if header_len <= 2:
            return 0
        if isinstance(buff, bytes):
            length_str = buff[2:header_len].decode('ascii')
        else:
            length_str = buff[2:header_len]
        return int(length_str)
    except (ValueError, IndexError):
        return 0


def expected_buff_bytes(buff):
    """Total expected bytes: TMC header + data payload + 1-byte terminator."""
    return tmc_header_bytes(buff) + expected_data_bytes(buff) + 1


def get_memory_depth(tn):
    """Query :ACQuire:MDEPth? and return the sample count as int.

    :ACQuire:MDEPth? is the authoritative record length the waveform read batches
    over, so a bad/empty reply must NOT be papered over with a guessed default --
    that would make the caller read the wrong number of points and report success.
    Raises ValueError on an empty or non-numeric reply.
    """
    response = command(tn, ':ACQuire:MDEPth?').strip()
    if not response:
        raise ValueError("empty reply to :ACQuire:MDEPth?")
    return int(float(response))  # handles scientific notation e.g. '1.0000E+04'



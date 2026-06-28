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
"""

import select
import time


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
    return sock.recv(max_bytes)


def command(tn, scpi, timeout=15, binary_data=False):
    """Send a SCPI command or query and return the response.

    Returns a decoded string for text queries, or raw bytes for binary ones.
    Pass binary_data=True to force binary mode regardless of the SCPI string.
    """
    if scpi.endswith('?'):
        tn.write((scpi + "\n").encode("utf-8"))

        scpi_upper = scpi.upper()
        if binary_data or ':WAVEFORM:DATA?' in scpi_upper or ':DISPLAY:DATA?' in scpi_upper:
            # Bypass telnetlib IAC processing for binary data (see _raw_socket_recv).
            # Flush any bytes telnetlib may have buffered from prior text reads.
            tn.rawq = b''
            tn.irawq = 0
            tn.cookedq = b''
            try:
                response = b""
                start_time = time.time()
                # Allow up to max_idle_time of silence before the TMC header arrives.
                # Once the header is parsed we trust the global timeout instead, because
                # DHO firmware can pause >2 s before sending the trailing newline on
                # large RAW reads.
                max_idle_time = min(2.0, max(0.5, timeout / 4.0))
                last_data_time = start_time
                total_expected = None

                while time.time() - start_time < timeout:
                    try:
                        chunk = _raw_socket_recv(tn, 65536, poll_timeout=0.05)
                        if chunk:
                            response += chunk
                            last_data_time = time.time()

                            if not response.startswith(b'#'):
                                marker_index = response.find(b'#')
                                if marker_index >= 0:
                                    response = response[marker_index:]

                            if response.startswith(b'#') and len(response) >= 2:
                                length_digits = int(chr(response[1]))
                                header_length = 2 + length_digits

                                if len(response) >= header_length:
                                    total_expected = expected_buff_bytes(response)
                                    if len(response) >= total_expected:
                                        response = response[:total_expected]
                                        break
                        else:
                            if total_expected is not None and len(response) >= total_expected:
                                response = response[:total_expected]
                                break
                            if total_expected is None and time.time() - last_data_time > max_idle_time:
                                break
                            time.sleep(0.01)
                    except Exception:
                        time.sleep(0.01)

                if not response.startswith(b'#'):
                    raise TimeoutError("No TMC header received")

                header_length = tmc_header_bytes(response)
                data_length = expected_data_bytes(response)
                total_expected = header_length + data_length + 1

                if len(response) < header_length:
                    raise TimeoutError("Incomplete TMC header")

                if len(response) < total_expected:
                    raise TimeoutError(
                        f"Incomplete binary block: got {len(response)}/{total_expected} bytes"
                    )

                terminator = response[header_length + data_length:total_expected]
                if terminator not in (b'\n', b'\r'):
                    raise TimeoutError(
                        f"Invalid binary block terminator: {terminator!r}"
                    )

                return response[:total_expected]

            except Exception as e:
                raise RuntimeError(f"Binary data read failed: {e}")

        else:
            try:
                response = tn.read_until(b"\n", timeout)
                if response:
                    return response.decode("utf-8", errors='ignore').strip()
                else:
                    raise TimeoutError(f"No response received for query {scpi}")
            except Exception as e:
                raise RuntimeError(f"Text query failed for {scpi}: {e}")

    else:
        try:
            tn.write((scpi + "\n").encode("utf-8"))
            # Short settling delay for commands that change scope state. The
            # patterns are matched against the upper-cased SCPI string, so they
            # must themselves be upper-case (the original mixed-case patterns
            # never matched). :WAVeform:SOURce / :WAVeform:MODE / :WAVeform:FORMat
            # are included because the next :WAVeform: query/read (e.g.
            # :WAVeform:YREFerence?, :WAVeform:DATA?) depends on them having taken
            # effect; :WAVeform:STARt / :WAVeform:STOP are left out to keep reads fast.
            if any(cmd in scpi.upper() for cmd in (
                ':TRIGGER:', ':ACQUIRE:', ':CHANNEL:', ':TIMEBASE:',
                ':WAVEFORM:SOURCE', ':WAVEFORM:MODE', ':WAVEFORM:FORMAT',
            )):
                time.sleep(0.05)
            return ""
        except Exception as e:
            return "command error"


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
    Raises ValueError on an empty or non-numeric reply; the caller
    (``RigolDHO800.memory_depth``) turns that into a RigolScopeError.
    """
    response = command(tn, ':ACQuire:MDEPth?').strip()
    if not response:
        raise ValueError("empty reply to :ACQuire:MDEPth?")
    return int(float(response))  # handles scientific notation e.g. '1.0000E+04'



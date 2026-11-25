#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Driver for Rigol DHO800 / DHO900 series oscilloscopes over LXI TCP/SCPI.

Written directly against the *DHO800 Series Programming Guide* (EN), §3.28
(the ``:WAVeform`` commands). No VISA / pyvisa: a plain TCP socket on port 5555
(wrapped in a patched ``telnetlib.Telnet``) is all that is needed. Binary
waveform reads bypass telnetlib's IAC-byte processing — see
``rigol_functions._raw_socket_recv`` — which would otherwise silently drop any
0xFF sample byte.

Reading a displayed channel (MAXimum mode, scope already STOPped after a real
trigger -- §3.28.2: in the Stop state MAXimum mode returns the full captured
memory record behind the on-screen waveform):

    # scope must already be STOPped after a real trigger; once per read_channels() call:
    :TRIGger:STATus?              # STOP
    N = :ACQuire:MDEPth?          # captured-sample count (§3.3.2)
    :TIMebase:MAIN:SCALe? / :TIMebase:MAIN:OFFSet?
    # then per channel:
    :WAVeform:SOURce  CHANnel<n>
    :WAVeform:MODE    MAXimum
    :WAVeform:FORMat  WORD        # 2 bytes/point, little-endian uint16 (§3.28.3)
    :WAVeform:PREamble?           # format,type,points,count,xinc,xorig,xref,yinc,yorig,yref
    # batched until all N points are read (§3.28.5):
    :WAVeform:STARt <k> ; :WAVeform:STOP <k+window-1> ; :WAVeform:DATA?  ...
    :CHANnel<n>:SCALe? / :CHANnel<n>:OFFSet?

The DHO firmware caps a single ``:WAVeform:DATA?`` at a per-transfer point count
that varies by model, so ``read_channel`` reads in batches: ``:WAVeform:STARt`` /
``:WAVeform:STOP`` advance over windows whose size is that observed cap (taken
from the first chunk), until the whole ``:ACQuire:MDEPth?`` record is read --
adjacent blocks are consecutive (§3.28.5).

Calibration is read straight off the scope, after SOURce/MODE/FORMat are set. In
RAW/MAX-Stop mode YINCrement and YORigin have no closed form ("related to the
VerticalScale of the internal waveform", §3.28.9/.10), so they must come from the
scope. By default one ``:WAVeform:PREamble?`` supplies all six values; its
``<points>`` field is never used (it follows the session-persistent
``:WAVeform:POINts`` setting, not the record -- ``:ACQuire:MDEPth?`` is the record
length). Set ``RigolDHO800.calibration_from_preamble = False`` to use the six
individual ``:WAVeform:X*?/Y*?`` queries instead (the pre-0.5 path; the 2026-05
firmware 00.01.03 notes say the preamble read wrong values in MAX mode, so the
bench test compares both). Conversion is the literal guide formula:

    voltage[i] = (raw[i] - y_origin - y_reference) * y_increment   # p.404
    time[i]    = x_origin + (i - x_reference) * x_increment        # §3.28.6/.7/.8

Public surface:
    RigolDHO800(ip, port=5555, timeout=5.0, verbose=True, deadline=None)   # context manager
        deadline                         Deadline capping every wait; replaceable between operations
        tcp_nodelay                      class attribute, True: no Nagle delay between commands
        idn / model
        run() / stop() / single() / set_sweep(mode)
        trigger_status()                 -> 'TD'|'WAIT'|'RUN'|'AUTO'|'STOP'
        wait_until_stopped(timeout)      -> raises ScopeTimeoutError on timeout
        displayed_channels()             -> ('CHANnel1', ...)   (analog only)
        memory_depth()                   -> int    (:ACQuire:MDEPth?)
        sample_rate()                    -> float  (:ACQuire:SRATe?)
        vertical_scale(ch) / vertical_offset(ch)
        timebase_scale() / timebase_offset()
        read_channels(chs, fmt='WORD')   -> {ch: Waveform}   (one shot, several channels)
        read_channel(ch, fmt='WORD')     -> Waveform   (read_channels for one channel)
        screen_png(path)                 -> path   (:DISPlay:DATA? PNG dump)

    Waveform        dataclass: channel, raw, voltage, time, metadata

Errors: transport failures raise lab_scopes.errors ScopeTimeoutError /
ScopeConnectionError / ScopeProtocolError; device-state problems raise RigolScopeError.
All are ScopeError (a RuntimeError). After a transport failure the connection is closed
and every later call raises ScopeConnectionError: port 5555 has no device clear, so a
late reply would otherwise answer the next query. Reconnect with a new instance.

Closing does not discard that late reply either: the DHO804 sends it on the NEXT
connection (bench, fw 00.01.05). So the constructor resynchronises
(``rigol_functions.resync``): it discards pending bytes, sends ``*IDN?`` and skips
everything until the ``RIGOL TECHNOLOGIES,`` line. ``stale_bytes_discarded`` says how
many bytes were skipped.
"""

import socket
import time
from dataclasses import dataclass

import numpy as np

from lab_scopes.errors import ScopeConnectionError, ScopeError, ScopeTimeoutError
from lab_scopes.transports.deadline import clamp, sleep
from lab_scopes.transports.rigol_functions import (
    command,
    expected_data_bytes,
    resync,
    tmc_header_bytes,
)
from lab_scopes.transports.telnetlib_receive_all import Telnet


# Native Rigol header storage size used by the HDF5 archive code.
RIGOL_WAVEDESC_SIZE = 256

# NORMal/RUN-mode sanity-check constant only (DHO800 Programming Guide §3.28.9):
# in those modes YINCrement == VerticalScale / 25. Not used for the conversion in
# MAX/Stop mode, where YINCrement is read straight from the scope.
BYTE_CODES_PER_DIV = 25.0


@dataclass
class Waveform:
    """One acquired trace plus its calibration metadata."""

    channel: str             # 'CHANnel1' .. 'CHANnel4'
    raw: np.ndarray          # scope codes: uint8 (BYTE) or little-endian uint16 (WORD)
    voltage: np.ndarray      # volts, float64, same length as raw
    time: np.ndarray         # seconds relative to trigger, float64, same length
    metadata: dict           # derived acquisition metadata

    @property
    def points(self):
        return len(self.voltage)


class RigolScopeError(ScopeError):
    """Raised for protocol / state errors talking to the scope."""


class RigolDHO800:
    """Rigol DHO800/DHO900 oscilloscope over LXI TCP/SCPI (port 5555)."""

    _ANALOG_CHANNELS = ('CHANnel1', 'CHANnel2', 'CHANnel3', 'CHANnel4')
    _MATH_CHANNELS = ('MATH1', 'MATH2', 'MATH3', 'MATH4')

    # Class-level so instances built without __init__ (test fakes) have no deadline.
    deadline = None

    # TCP_NODELAY on the socket. With Nagle on, a write that follows an unanswered write
    # (:WAVeform:SOURce, MODE, FORMat, STARt, STOP) waits for the scope's delayed ACK.
    # Bench 2026-11-25: about 24 ms per command, ~0.39 s of a 1.17 s C1+C2 read. The
    # LeCroy transport sets it for the same reason. False restores the v0.4.x socket.
    tcp_nodelay = True

    # -- construction / lifetime -------------------------------------------- #

    def __init__(self, ip, port=5555, timeout=5.0, verbose=True, deadline=None):
        self.ip = ip
        self.port = port
        self.timeout = timeout  # connect and send ceiling
        self.verbose = verbose
        self.deadline = deadline

        self.tn = None
        self.idn = ''
        self.stale_bytes_discarded = 0
        self.manufacturer = self.model = self.serial = self.firmware = ''

        if self.verbose:
            print(f"<:> connecting to Rigol scope at {ip}:{port}")
        try:
            self.tn = Telnet(ip, port, timeout=clamp(deadline, timeout))
        except OSError as exc:
            raise ScopeConnectionError(f"cannot connect to scope at {ip}:{port}: {exc}") from exc
        # The connect may have used a deadline-shortened timeout; sends keep the full ceiling.
        self.tn.timeout = timeout
        self.tn.sock.settimeout(timeout)
        if self.tcp_nodelay:
            try:
                self.tn.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError as exc:
                self.close()
                raise ScopeConnectionError(f"cannot set TCP_NODELAY on {ip}:{port}: {exc}") from exc

        # Resynchronise: a reply cut off on an earlier connection arrives on this one.
        try:
            self.idn, self.stale_bytes_discarded = resync(self.tn, timeout=15, deadline=deadline)
        except BaseException:
            self.close()
            raise
        if self.verbose and self.stale_bytes_discarded:
            print(f"<:> discarded {self.stale_bytes_discarded} stale bytes from an earlier connection")
        parts = [p.strip() for p in self.idn.split(',')]
        if len(parts) >= 4:
            self.manufacturer, self.model, self.serial, self.firmware = parts[:4]
        if self.verbose:
            print(f"<:> {self.idn}")

    def __repr__(self):
        return f"RigolDHO800({self.ip!r}, model={self.model!r})"

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def close(self):
        """Close the TCP connection."""
        if self.tn is not None:
            try:
                self.tn.close()
            finally:
                self.tn = None
                if self.verbose:
                    print("<:> disconnected from Rigol scope")

    @property
    def connected(self):
        return self.tn is not None

    # -- low-level SCPI helpers --------------------------------------------- #

    def _io(self, *args, **kwargs):
        """``command(self.tn, ...)`` under ``self.deadline``; any failure closes the connection."""
        if self.tn is None:
            raise ScopeConnectionError(f"connection to {self.ip} is closed (closed, or an earlier I/O failure)")
        try:
            return command(self.tn, *args, deadline=self.deadline, **kwargs)
        except BaseException:
            self.close()
            raise

    def _query(self, scpi, timeout=15):
        """Send a query and return the stripped text reply."""
        return self._io(scpi, timeout=timeout)

    def _query_float(self, scpi, default=None):
        """``default`` covers an unparseable reply only; transport errors propagate."""
        reply = self._query(scpi)
        try:
            return float(reply)
        except ValueError:
            if default is None:
                raise RigolScopeError(f"non-numeric reply to {scpi}: {reply!r}")
            return default

    def _query_int(self, scpi, default=None):
        return int(self._query_float(scpi, default))

    def _write(self, scpi):
        """Send a command (no reply expected)."""
        self._io(scpi)

    def _read_block(self, scpi, timeout):
        """Send a query that returns an IEEE 488.2 definite-length block.

        Returns ``(data_bytes, declared_len)`` -- the payload (header & trailing
        terminator stripped) and the byte count the block header declared.
        """
        resp = self._io(scpi, timeout=timeout, binary_data=True)
        hdr_len = tmc_header_bytes(resp)
        declared = expected_data_bytes(resp)
        if declared <= 0:
            raise RigolScopeError(f"empty TMC block in reply to {scpi!r}")
        return resp[hdr_len:hdr_len + declared], declared

    # -- channel-name normalisation ----------------------------------------- #

    @staticmethod
    def channel_name(ch):
        """Normalise 'C1' / 1 / 'CHAN1' / 'CHANnel1' -> 'CHANnel1'. Also passes MATH<n>."""
        if isinstance(ch, int):
            if 1 <= ch <= 4:
                return f'CHANnel{ch}'
            raise ValueError(f"channel index out of range: {ch}")
        s = str(ch).strip().upper()
        if s.startswith('MATH'):
            return s.replace('MATH', 'MATH')  # already canonical-ish; pass through
        for prefix in ('CHANNEL', 'CHAN', 'CH', 'C'):
            if s.startswith(prefix) and s[len(prefix):].isdigit():
                n = int(s[len(prefix):])
                if 1 <= n <= 4:
                    return f'CHANnel{n}'
        raise ValueError(f"unrecognised channel: {ch!r}")

    @staticmethod
    def short_channel_name(ch):
        """'CHANnel1' -> 'C1' (the form the HDF5/archive code keys on)."""
        full = RigolDHO800.channel_name(ch) if not str(ch).upper().startswith('MATH') else str(ch).upper()
        if full.startswith('CHANnel'):
            return f'C{full[-1]}'
        return full

    # -- run / stop / trigger ----------------------------------------------- #

    def run(self):
        self._write(':RUN')

    def stop(self):
        self._write(':STOP')

    def single(self):
        self._write(':SINGle')

    def set_sweep(self, mode):
        """Set the trigger sweep mode: 'AUTO' or 'NORMal'."""
        m = str(mode).strip().upper()
        if m.startswith('AUTO'):
            self._write(':TRIGger:SWEep AUTO')
        elif m.startswith('NORM'):
            self._write(':TRIGger:SWEep NORMal')
        else:
            raise ValueError(f"unsupported sweep mode: {mode!r} (use 'AUTO' or 'NORMal')")

    def trigger_status(self):
        """Return the acquisition state: 'TD', 'WAIT', 'RUN', 'AUTO' or 'STOP'."""
        return self._query(':TRIGger:STATus?').strip().upper()

    def wait_until_stopped(self, timeout=30.0, poll_interval=0.05):
        """Block until ``:TRIGger:STATus?`` reports STOP, or raise ``ScopeTimeoutError``."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if self.trigger_status() == 'STOP':
                return
            sleep(self.deadline, poll_interval)
        raise ScopeTimeoutError(f"scope did not reach STOP within {timeout}s")

    # -- configuration queries ---------------------------------------------- #

    def displayed_channels(self):
        """Return the analog channels currently shown on screen, e.g. ('CHANnel1', 'CHANnel3')."""
        # Not try/except per channel: after a failure the connection is closed, and every
        # later channel would silently read as "not displayed".
        shown = [ch for ch in self._ANALOG_CHANNELS
                 if self._query(f':{ch}:DISPlay?').strip() == '1']
        # This driver acquires analog channels only; note (don't error) if MATH is on.
        math_on = [ch for ch in self._MATH_CHANNELS
                   if self._query(f':{ch}:DISPlay?').strip() == '1']
        if math_on:
            print(f"NOTE: MATH trace(s) {math_on} are displayed but are not acquired "
                  f"by this driver (analog channels only).")
        return tuple(shown)

    def memory_depth(self):
        """Return ``:ACQuire:MDEPth?`` as int -- the captured-sample count per trigger.

        The query returns scientific notation (e.g. ``1.000E+6``), even in Auto
        mode (Programming Guide §3.3.2). This is the authoritative record length, so a
        bad reply raises RigolScopeError rather than defaulting.
        """
        return self._query_int(':ACQuire:MDEPth?')

    def sample_rate(self):
        """Return ``:ACQuire:SRATe?`` (Sa/s). Raises if the scope gives a non-positive
        value -- the MAX/Stop time axis is ``1/SampleRate``, so a bad rate is fatal."""
        srate = self._query_float(':ACQuire:SRATe?')
        if srate <= 0:
            raise RigolScopeError(f"scope reported sample rate {srate}; cannot build time axis")
        return srate

    def vertical_scale(self, ch):
        return self._query_float(f':{self.channel_name(ch)}:SCALe?')

    def vertical_offset(self, ch):
        return self._query_float(f':{self.channel_name(ch)}:OFFSet?', default=0.0)

    def timebase_scale(self):
        return self._query_float(':TIMebase:MAIN:SCALe?', default=0.0)

    def timebase_offset(self):
        return self._query_float(':TIMebase:MAIN:OFFSet?', default=0.0)

    # -- the waveform read -------------------------------------------------- #

    # Per-chunk :WAVeform:DATA? timeout model: a fixed floor plus a per-MB
    # allowance. Scaled by BYTES, not points, because WORD is 2 bytes/point -- a
    # WORD chunk transfers twice the bytes of a BYTE chunk of the same point count.
    # The per-MB figure is deliberately conservative: it bounds a single transfer,
    # not the whole record, and a deep/slow read can pause before the trailing
    # newline (see rigol_functions.command). Tune _WAVEFORM_TIMEOUT_S_PER_MB from a
    # measured worst-case 25M WORD read if a deep transfer ever times out.
    _WAVEFORM_TIMEOUT_FLOOR_S = 15
    _WAVEFORM_TIMEOUT_BASE_S = 5
    _WAVEFORM_TIMEOUT_S_PER_MB = 5

    @classmethod
    def _waveform_read_timeout(cls, window_bytes):
        """Per-chunk socket timeout (s) for a ``:WAVeform:DATA?`` of this byte size."""
        per_mb = window_bytes // 1_000_000 * cls._WAVEFORM_TIMEOUT_S_PER_MB
        return max(cls._WAVEFORM_TIMEOUT_FLOOR_S,
                   cls._WAVEFORM_TIMEOUT_BASE_S + per_mb)

    # A :WAVeform:DATA? that returns no points is usually the scope being
    # momentarily busy, not the end of the record -- retry a few times with a short
    # backoff before treating it as terminal (a genuinely stalled scope still bails).
    _EMPTY_CHUNK_RETRIES = 3
    _EMPTY_CHUNK_BACKOFF = 0.1

    def _read_full_waveform(self, n_total, bytes_per_point, verify_window=False):
        """Read ``n_total`` waveform points via batched ``:WAVeform:DATA?``.

        Assumes ``:WAVeform:SOURce`` / ``:WAVeform:MODE`` / ``:WAVeform:FORMat``
        are already set. ``bytes_per_point`` is 1 for BYTE, 2 for WORD (§3.28.3).
        The DHO firmware caps a single ``:WAVeform:DATA?`` at some per-transfer
        point count that varies by model; this loop discovers that cap from the
        first chunk and then advances ``:WAVeform:STARt`` / ``:WAVeform:STOP`` over
        windows of that size until the whole record is read (Programming Guide
        §3.28.5 -- "read in batches", adjacent blocks are consecutive).

        ``:WAVeform:STARt`` / ``:WAVeform:STOP`` and the window cap are counted in
        **points**, while ``:WAVeform:DATA?`` returns **bytes**, so each chunk's
        byte length is converted to points (``bytes // bytes_per_point``) before
        advancing. Returns the concatenated payload bytes (``n_total *
        bytes_per_point`` when complete).

        An empty chunk is retried (``_EMPTY_CHUNK_RETRIES``) before being treated as
        terminal; if it still yields no points, the loop stops and returns whatever
        it has (the caller raises on a short record). Any positive return advances
        ``start``, so progress always terminates the loop.

        With ``verify_window=True`` each window's ``:WAVeform:STARt?/:STOP?`` is read
        back and a mismatch raises ``RigolScopeError``. This guards the historical
        firmware bug where a large STOP clamped STARt away from the requested value
        (so the record silently shifted). Off by default -- it adds two queries per
        chunk -- enable it for diagnostics or near the memory-depth boundary.
        """
        chunks = []
        start = 1
        window = n_total  # window cap, in points
        first = True
        empty_tries = 0
        while start <= n_total:
            stop = min(start + window - 1, n_total)
            self._write(f':WAVeform:STARt {start}')
            self._write(f':WAVeform:STOP {stop}')
            if verify_window:
                got_start = self._query_int(':WAVeform:STARt?')
                got_stop = self._query_int(':WAVeform:STOP?')
                if (got_start, got_stop) != (start, stop):
                    raise RigolScopeError(
                        f"firmware clamped read window: asked STARt {start}/STOP "
                        f"{stop}, got STARt {got_start}/STOP {got_stop}"
                    )
            payload, declared = self._read_block(
                ':WAVeform:DATA?',
                timeout=self._waveform_read_timeout(window * bytes_per_point),
            )
            got_bytes = min(len(payload), declared)
            points_got = got_bytes // bytes_per_point  # whole points only
            if points_got <= 0:
                empty_tries += 1
                if empty_tries > self._EMPTY_CHUNK_RETRIES:
                    break  # scope made no progress after retries; return partial
                if self.verbose:
                    print(f"   empty :WAVeform:DATA? at point {start}, "
                          f"retry {empty_tries}/{self._EMPTY_CHUNK_RETRIES}")
                sleep(self.deadline, self._EMPTY_CHUNK_BACKOFF)
                continue
            empty_tries = 0  # progress made; reset the retry budget
            chunks.append(payload[:points_got * bytes_per_point])
            if first and points_got < n_total:
                window = points_got  # adopt the firmware's observed per-transfer cap
                if self.verbose:
                    print(f"   learned per-transfer cap: {window} points/chunk "
                          f"({window * bytes_per_point} bytes)")
            first = False
            start += points_got
        return b''.join(chunks)

    # Bytes per sample for each supported :WAVeform:FORMat (Programming Guide §3.28.3).
    _BYTES_PER_POINT = {'BYTE': 1, 'WORD': 2}
    # <format> field of :WAVeform:PREamble?: 0=BYTE, 1=WORD, 2=ASCii.
    _PREAMBLE_FORMAT = {'BYTE': 0, 'WORD': 1}

    # True: one :WAVeform:PREamble? per channel. False: the six individual
    # :WAVeform:X*?/Y*? queries (pre-0.5 behaviour). Both give the same six floats.
    calibration_from_preamble = True

    def _check_format(self, fmt):
        fmt_up = str(fmt).strip().upper()
        if fmt_up not in self._BYTES_PER_POINT:
            raise RigolScopeError(
                f"unsupported waveform format {fmt!r}; use 'WORD' (2 bytes/point) or 'BYTE'"
            )
        return fmt_up

    def _select_waveform(self, ch, fmt_up):
        """Point the :WAVeform: subsystem at ``ch`` in MAXimum mode and ``fmt_up`` format.

        No settle delay: SCPI runs commands in order, so the next query already sees the
        new source, mode and format. MAXimum mode in the Stop state returns the full
        captured memory record (§3.28.2).
        """
        self._write(f':WAVeform:SOURce {ch}')
        self._write(':WAVeform:MODE MAXimum')
        self._write(f':WAVeform:FORMat {fmt_up}')

    def _calibration_from_queries(self):
        """The six calibration values from the individual :WAVeform:X*?/Y*? queries
        (the "Related Commands" for :WAVeform:DATA?, p.404)."""
        return {
            'x_increment': self._query_float(':WAVeform:XINCrement?'),
            'x_origin': self._query_float(':WAVeform:XORigin?'),
            'x_reference': self._query_float(':WAVeform:XREFerence?', default=0.0),
            'y_increment': self._query_float(':WAVeform:YINCrement?'),
            'y_origin': self._query_float(':WAVeform:YORigin?'),
            'y_reference': self._query_float(':WAVeform:YREFerence?'),
        }

    def _calibration_from_preamble(self, fmt_up):
        """The six calibration values from one ``:WAVeform:PREamble?``.

        Reply: ``format,type,points,count,xinc,xorig,xref,yinc,yorig,yref``. Only the
        last six are used; ``<format>`` must match ``fmt_up`` (a mismatch would mean the
        FORMat command was not applied). ``<points>`` is ignored: it is not the record
        length (see the module docstring).
        """
        reply = self._query(':WAVeform:PREamble?')
        fields = reply.split(',')
        try:
            values = [float(f) for f in fields]
        except ValueError:
            values = None
        if values is None or len(values) != 10:
            raise RigolScopeError(f"malformed reply to :WAVeform:PREamble?: {reply!r}")
        if values[0] != self._PREAMBLE_FORMAT[fmt_up]:
            raise RigolScopeError(
                f":WAVeform:PREamble? reports format {fields[0].strip()}, "
                f"expected {self._PREAMBLE_FORMAT[fmt_up]} ({fmt_up})"
            )
        keys = ('x_increment', 'x_origin', 'x_reference', 'y_increment', 'y_origin', 'y_reference')
        return dict(zip(keys, values[4:]))

    def _read_calibration(self, fmt_up):
        if self.calibration_from_preamble:
            return self._calibration_from_preamble(fmt_up)
        return self._calibration_from_queries()

    def read_channels(self, channels, fmt='WORD', verify_window=False):
        """Read several displayed analog channels of one stopped acquisition.

        Returns ``{channel: Waveform}`` keyed by the ``channels`` items exactly as passed
        (``read_channels(['C1', 'C2'])['C1']``), in that order. Each ``Waveform`` is what
        ``read_channel`` returns for that channel.

        Once per call: one stopped check (``wait_until_stopped``), ``:ACQuire:MDEPth?``,
        ``:TIMebase:MAIN:SCALe?`` and ``:TIMebase:MAIN:OFFSet?``. Then per channel:
        source / MAXimum mode / format, the calibration (``:WAVeform:PREamble?``), the
        batched ``:WAVeform:DATA?`` read (``_read_full_waveform`` -- the firmware caps a
        single transfer, so this loops ``:WAVeform:STARt`` / ``:WAVeform:STOP``
        windows until done), ``:CHANnel<n>:SCALe?`` and ``:CHANnel<n>:OFFSet?``.

        ``fmt`` defaults to ``'WORD'``: 2 bytes/point, little-endian uint16 (§3.28.3).
        On the DHO804 (fw 00.01.05) WORD codes are not 0..4095: they sit around
        ``:WAVeform:YREFerence?`` (32768), spanning roughly 15,600..50,500 with about
        750 unevenly spaced levels per channel. ``'BYTE'`` (1 byte/point) is also
        accepted but keeps about 4 fewer bits -- WORD is the right choice for data
        acquisition. Conversion uses the literal guide formula
        ``voltage = (raw - YORigin - YREFerence) * YINCrement``.

        ``verify_window=True`` reads each batch's ``:WAVeform:STARt?/:STOP?`` back and
        raises on a mismatch (a diagnostic guard against firmware window clamping;
        adds two queries per chunk, so it is off by default).

        The scope must already be STOPped: in the Stop state MAXimum mode reads
        the captured internal-memory record behind the on-screen waveform
        (Programming Guide §3.28.2). Raises ``RigolScopeError`` on any
        state/protocol problem.
        """
        fmt_up = self._check_format(fmt)
        targets = {}
        for key in channels:
            ch = self.channel_name(key)
            if key in targets or ch in targets.values():
                raise ValueError(f"channel {key!r} requested twice")
            targets[key] = ch
        if not targets:
            raise ValueError("no channels to read")

        self.wait_until_stopped()

        # MDEPth is the captured-sample count (§3.3.2); MAX/Stop STARt..STOP range
        # is 1..memory_depth (§3.28.13). Same for every channel of the acquisition.
        n_mdepth = self.memory_depth()
        if n_mdepth <= 0:
            raise RigolScopeError(f"scope reported memory depth {n_mdepth}; nothing to read")
        tb_scale = self.timebase_scale()
        tb_offset = self.timebase_offset()

        return {key: self._read_one_channel(ch, fmt_up, n_mdepth, tb_scale, tb_offset, verify_window)
                for key, ch in targets.items()}

    def read_channel(self, channel, fmt='WORD', verify_window=False):
        """Read one displayed analog channel in MAXimum mode. Returns a ``Waveform``.

        ``read_channels([channel], ...)`` for a single channel; see there.
        """
        return self.read_channels([channel], fmt=fmt, verify_window=verify_window)[channel]

    def _read_one_channel(self, ch, fmt_up, n_mdepth, tb_scale, tb_offset, verify_window):
        bytes_per_point = self._BYTES_PER_POINT[fmt_up]
        t0 = time.time()
        if self.verbose:
            print(f"<:> reading {ch} ({fmt_up}) from internal memory (MAXimum/Stop)")

        self._select_waveform(ch, fmt_up)
        # Describes the whole record -- read once, before the batched read, and after
        # the format is set so the scope reports the scaling for the format in effect.
        cal = self._read_calibration(fmt_up)

        # Fetch the full record in batches (§3.28.5). DATA? returns bytes;
        # bytes_per_point converts to the point count.
        payload = self._read_full_waveform(n_mdepth, bytes_per_point, verify_window)
        n = len(payload) // bytes_per_point
        if n <= 0:
            raise RigolScopeError(f"no usable samples for {ch}: got {len(payload)} data bytes")
        if n < n_mdepth:
            raise RigolScopeError(
                f"incomplete waveform for {ch}: got {n} of {n_mdepth} memory points"
            )

        # BYTE -> uint8, WORD -> little-endian uint16.
        dtype = np.dtype('<u2') if fmt_up == 'WORD' else np.dtype(np.uint8)
        raw = np.frombuffer(payload[:n * bytes_per_point], dtype=dtype)

        # Conversion -- literal guide formula (p.404):
        #   voltage = (raw - YORigin - YREFerence) * YINCrement
        #   time    = XORigin + (i - XREFerence) * XINCrement   (XREFerence == 0)
        voltage = (raw.astype(np.float64) - cal['y_origin'] - cal['y_reference']) * cal['y_increment']
        idx = np.arange(n, dtype=np.float64)
        t_arr = cal['x_origin'] + (idx - cal['x_reference']) * cal['x_increment']

        # Extra context for the HDF5 attrs / sanity checks (not used for conversion).
        v_scale = self.vertical_scale(ch)
        v_offset = self.vertical_offset(ch)

        metadata = {
            'format': 0 if fmt_up == 'BYTE' else 1,   # 0=BYTE, 1=WORD (§3.28.3)
            'type': 1,     # MAXimum (§3.28.14)
            'points': n,
            'count': 1,
            'x_increment': cal['x_increment'],
            'x_origin': cal['x_origin'],
            'x_reference': cal['x_reference'],
            'y_increment': cal['y_increment'],
            'y_origin': cal['y_origin'],
            'y_reference': cal['y_reference'],
            'vertical_scale': v_scale,
            'vertical_offset': v_offset,
            'timebase_scale': tb_scale,
            'timebase_offset': tb_offset,
        }
        if self.verbose:
            self._sanity_warnings(ch, metadata, voltage)
            print(f"   {n} samples, dt={cal['x_increment']:.4g}s, "
                  f"V[{voltage.min():.4g}, {voltage.max():.4g}] "
                  f"({time.time() - t0:.2f}s)")

        return Waveform(channel=ch, raw=raw, voltage=voltage, time=t_arr, metadata=metadata)

    def _sanity_warnings(self, ch, metadata, voltage):
        """Print hints for the two classic failure modes (verbose only)."""
        # (a) scope-reported YINCrement vs VerticalScale/25 -- exact in NORM/RUN
        # (§3.28.9), only "related" in MAX/Stop, so this is informational: a wild
        # ratio usually means a stale/wrong V/div read or a mode mix-up.
        v_div = metadata['vertical_scale']  # already read; no second query
        expected = v_div / BYTE_CODES_PER_DIV
        if expected > 0 and metadata['y_increment'] > 0:
            ratio = metadata['y_increment'] / expected
            if ratio < 0.5 or ratio > 2.0:
                print(f"   NOTE: {ch} reported YINCrement={metadata['y_increment']:.4g}V "
                      f"is {ratio:.2g}x VerticalScale/25 ({expected:.4g}V from "
                      f"{v_div:g}V/div) -- expected in MAX/Stop, but worth a glance")
        # (b) a long constant tail -> the acquisition probably didn't capture a full record.
        if voltage.size >= 64:
            tail = voltage[-max(64, voltage.size // 50):]
            if tail.size and np.ptp(tail) == 0.0:
                print(f"   WARNING: {ch} ends with {tail.size}+ identical samples "
                      f"(~{float(tail[0]):.4g} V) -- the acquisition may not have "
                      f"captured a full record (no/late trigger, or :ACQuire:MDEPth larger "
                      f"than the timebase captures)")

    # -- screenshot --------------------------------------------------------- #

    def screen_png(self, path):
        """Save the current display as a PNG (``:DISPlay:DATA?``). Returns ``path``."""
        data, declared = self._read_block(':DISPlay:DATA?', timeout=15)
        png = data[:declared]
        if not png.startswith(b'\x89PNG'):
            raise RigolScopeError(f"display data is not a PNG (first bytes: {png[:8]!r})")
        with open(path, 'wb') as fh:
            fh.write(png)
        if self.verbose:
            print(f"<:> screenshot saved to {path} ({len(png)} bytes)")
        return path

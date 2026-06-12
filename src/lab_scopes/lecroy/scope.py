"""LeCroy oscilloscope driver without PyVISA.

This is based on the LAPD_DAQ ``LeCroy_Scope.py`` driver surface, but the
connection is a native VICP transport instead of VISA.

TODO: Sequence-mode acquisition is broken in this version. ``acquire_sequence_data``
raises, and the sequence branches in ``parse_wavedesc`` / ``time_array`` are kept
commented out (as a restoration reference) until the segment read path is fixed.
See tests/test_lecroy_scope_acquire.py and tests/test_lecroy_scope_real.py for the
corresponding disabled tests.
"""

from __future__ import annotations

import struct
import sys
import time
from typing import Tuple

import numpy as np

from lab_scopes.errors import ScopeConnectionError
from lab_scopes.transports.lecroy_vicp import LeCroyVICPTransport

from .constants import (
    EXPANDED_TRACE_NAMES,
    KNOWN_TRACE_NAMES,
    PROCESSING_TYPES,
    RECORD_TYPES,
    TIMEBASE_IDS,
    VERT_COUPLINGS,
    VERT_GAIN_IDS,
    WAVEDESC,
    WAVEDESC_FMT,
    WAVEDESC_SIZE,
)


# Byte offset of the WAVEDESC ``sweeps_per_acq`` (int32) field within a
# :WAVEFORM? response: 15-byte VICP/command preamble + 148-byte offset into the
# WAVEDESC. Used as a monotonic completed-sweep counter to detect a *fresh*
# acquisition (after CLEAR_SWEEPS) rather than relying on the ambiguous STOP state.
SWEEPS_PER_ACQ_OFFSET = 15 + 148


# Bit in the LeCroy X-Stream INR (Internal state change Register, read via
# "INR?") that means "trigger is ready / waiting for a trigger". Unlike
# "TRIG_MODE?" -- which reports the configured mode and flips to SIN before the
# trigger subsystem has finished re-arming -- this bit reflects the trigger
# engine actually being armed and listening, so it is the race-free signal that a
# scope (e.g. a slave) is ready to capture an edge. INR is read-to-clear.
INR_TRIGGER_READY = 0x2000


# Substrings (case-insensitive) of the *IDN? model field that identify LeCroy
# instruments with 8 analog input channels (C1-C8). Used to skip the probe-based
# channel-count fallback when the model is recognized -- a zero-round-trip fast
# path. This list is intentionally conservative and extensible: an 8-channel
# model NOT listed here simply falls back to a single C5 probe (see
# _detect_channel_count), so a missing entry costs one extra round-trip, never
# wrong behavior. Add new 8-channel families here as they are verified on
# hardware. Known 8-ch LeCroy families: WaveRunner 8000HD, HDO8000/8000A,
# WavePro HD (8-ch variants).
LECROY_8CH_MODEL_MARKERS = (
    "8000HD",
    "HDO8",
    "WAVEPRO HD",
    "WR8",
)


class LeCroyNoDataError(RuntimeError):
    """Raised when a trace's :WAVEFORM? response carries no curve data.

    Why: math/measurement traces can be "ON" with no source assigned, so they
    accept :WAVEFORM? but return a payload too short to contain a WAVEDESC.
    """


def _vbs_int(resp, default: int = 0) -> int:
    # MAUI VBS reads on inactive math/measurement traces return the literal "OFF".
    s = resp.strip() if isinstance(resp, str) else resp
    if not s or (isinstance(s, str) and s.upper() == "OFF"):
        return default
    try:
        return int(s)
    except (TypeError, ValueError):
        return default


def _vbs_float(resp, default: float = 0.0) -> float:
    s = resp.strip() if isinstance(resp, str) else resp
    if not s or (isinstance(s, str) and s.upper() == "OFF"):
        return default
    try:
        return float(s)
    except (TypeError, ValueError):
        return default


class LeCroyScope:
    """LeCroy X-Stream scope driver using native TCP/VICP."""

    valid_trace_names = ()
    gaaak_count = 0
    idn_string = ""
    trace_bytes = np.zeros(shape=(WAVEDESC_SIZE), dtype="b")
    offscale_fraction = 0.005
    # 4-channel defaults; __init__ overrides these per detected hardware. Present
    # at class scope so fakes built via __new__ (skipping __init__) still work.
    n_channels = 4
    channel_names = ("C1", "C2", "C3", "C4")

    def __init__(self, ipv4_addr: str, verbose: bool = True, timeout: float = 5.0,
                 port: int = 1861, transport=None, discover_traces="channels"):
        self.ipv4_addr = ipv4_addr
        self.verbose = verbose
        self.transport = transport or LeCroyVICPTransport(ipv4_addr, port=port, timeout=_seconds(timeout))
        self.scope = self.transport  # compatibility alias used by LAPD_DAQ
        self.rm = None               # old VISA attribute; retained as inert compatibility state
        self.rm_status = self.rm_open(ipv4_addr)
        if not self.rm_status:
            raise RuntimeError("**** program exiting")

        self.scope.timeout = _seconds(timeout)
        self.scope.chunk_size = 1000000
        self.scope.write("COMM_HEADER OFF")
        self.scope.write("COMM_FORMAT DEF9,WORD,BIN")

        # Determine how many analog input channels this scope has (4 or 8) before
        # probing any trace names, so we never probe channels that cannot exist.
        self.n_channels = self._detect_channel_count()
        self.channel_names = tuple(f"C{i}" for i in range(1, self.n_channels + 1))

        # Discover which trace names this scope actually accepts.
        #
        # Probing a name costs two synchronous VICP round-trips (:TRACE? + CMR?),
        # and an *invalid* name (e.g. math/memory traces, or C5-C8 on a 4-channel
        # scope) can stall up to the full socket timeout. Probing all 20+
        # KNOWN_TRACE_NAMES therefore made __init__ hang for many seconds. Default
        # to just the detected input channels, which is what almost all callers
        # use and which are all guaranteed valid (zero timeouts); pass
        # discover_traces="all" to probe the full math/memory/letter set, or an
        # explicit tuple of names to probe just those.
        if discover_traces == "channels":
            candidates = self.channel_names
        elif discover_traces == "all":
            candidates = tuple(KNOWN_TRACE_NAMES)
        else:
            candidates = tuple(discover_traces)
        # Clear the slate so the first candidate -- always C1 -- is never blamed
        # for an error latched by earlier traffic (e.g. an aborted detection
        # probe) and silently dropped.
        self._clear_status()
        # Instance attribute (shadows the empty class default) so two scopes with
        # different configurations never share a discovered list.
        self.valid_trace_names = ()
        for tr in candidates:
            try:
                self.scope.query(tr + ":TRACE?")
                error_code = int(self.scope.query("CMR?"))
            except Exception as exc:
                reason = f"{type(exc).__name__}: {exc}"
            else:
                if error_code == 0:
                    self.valid_trace_names += (tr,)
                    continue
                reason = f"CMR={error_code}"
            if tr in self.channel_names:
                # A dropped input channel is lost data for the whole run; it
                # must be visible at run start, not discovered as a missing
                # dataset in the HDF5 hours later.
                print(f"**** trace discovery: input channel {tr} rejected "
                      f"({reason}) -- it will not be acquired")
            elif self.verbose:
                print(f"<:> trace discovery: skipping {tr} ({reason})")

    def _detect_channel_count(self) -> int:
        """Return the number of analog input channels (4 or 8) on this scope.

        Two-tier strategy that avoids probing channels that cannot exist:

        1. Fast path -- parse the model from ``*IDN?`` (already captured in
           ``rm_open``). If it matches a known 8-channel family marker, return 8
           with zero extra round-trips.
        2. Fallback -- for an unrecognized model, do a single ``C5:TRACE?`` +
           ``CMR?`` probe. ``CMR? == 0`` means the scope accepted C5, so it has 8
           channels; a non-zero/non-numeric ``CMR?`` (or a read timeout, which a
           4-channel scope may return for an invalid channel) means 4. This costs
           one round-trip pair, far cheaper than the multi-second hang of blindly
           probing C5-C8.

        Defaults to 4 -- the safe lower bound (a 4-channel set is valid on every
        supported scope), so an ambiguous reply never invents channels that do
        not exist. A genuine connection drop (``ScopeConnectionError``) is *not*
        swallowed: silently returning 4 would mask a dead link as a quiet
        mis-detection, so it propagates and fails init loudly.
        """
        idn_upper = (self.idn_string or "").upper()
        if any(marker in idn_upper for marker in LECROY_8CH_MODEL_MARKERS):
            if self.verbose:
                print("<:> detected 8-channel scope from *IDN?")
            return 8
        # Unrecognized model: probe the 4-vs-8 boundary with one C5 query. A read
        # timeout / non-numeric CMR? is ambiguous -> treat as 4 (handled by the
        # broad except + _vbs_int default); a connection drop is fatal and is
        # re-raised before that.
        try:
            self.scope.query("C5:TRACE?")
            if _vbs_int(self.scope.query("CMR?"), default=-1) == 0:
                if self.verbose:
                    print("<:> detected 8-channel scope from C5 probe")
                return 8
        except ScopeConnectionError:
            raise
        except Exception:
            # The probe died before its CMR? read could clear the register
            # (on real 4-channel hardware an invalid trace query sends *no
            # reply*, so the read times out with the error bit latched).
            self._clear_status()
        return 4

    def _clear_status(self):
        """Best-effort ``*CLS`` -- clear the scope's latched status registers.

        CMR (the command-error register) is read-to-clear and latches the most
        recent command error, so an errored command whose ``CMR?`` read never
        ran would be blamed on the *next* command -- e.g. dropping C1 from
        trace discovery after an aborted C5 detection probe. ``*CLS`` is a
        write (no reply to desync on); failures are swallowed because this
        runs on paths where the link may already be degraded.
        """
        try:
            self.scope.write("*CLS")
        except Exception:
            pass

    def __repr__(self):
        return repr(self.scope)

    def __str__(self):
        txt = repr(self.scope) + "\n"
        for tr in self.displayed_traces():
            txt += self.scope.query(tr + ":VOLT_DIV?")
        txt += self.scope.query("TIME_DIV?")
        txt += self.scope.query('VBS? "return=app.Acquisition.Horizontal.NumPoints"')
        return txt

    def __bool__(self):
        return self.rm_status

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        print("LeCroy_Scope:__exit__() called", end="")
        if self.gaaak_count != 0:
            print(" with", self.gaaak_count, '"gaaak" type errors', end="")
        print("  at", time.ctime())
        self.__del__()

    def __del__(self):
        if getattr(self, "scope", None) is not None:
            try:
                self.scope.close()
            finally:
                self.scope = None
        self.rm_status = False

    def rm_list_resources(self):
        """Compatibility stub; native TCP transport has no VISA resource list."""
        return ()

    def rm_open(self, ipv4_addr) -> bool:
        if self.scope is None:
            return False
        if not getattr(self.scope, "connected", False):
            if self.verbose:
                print(f"<:> attempting to open native VICP connection to {ipv4_addr}")
            self.scope.open()
        try:
            self.idn_string = self.scope.query("*IDN?")
            if self.verbose:
                print("<:>", self.idn_string)
        except Exception:
            print(f'\n**** Scope at "{ipv4_addr}" did not respond to "*IDN?" query\n')
            self.scope.close()
            return False
        return True

    def rm_close(self):
        if self.scope is not None:
            self.scope.close()

    def screen_dump(self, fig_name="scope_screen_dump", white_background=False,
                    png_fn="scope_screen_dump.png", full_screen=True, show_plot=True):
        bckg = "WHITE" if white_background else "BLACK"
        area = "DSOWINDOW" if full_screen else "GRIDAREAONLY"
        self.scope.write("COMM_HEADER OFF")
        self.scope.write(f'HARDCOPY_SETUP DEV, PNG, BCKG, {bckg}, DEST, "REMOTE", AREA, {area}')
        self.scope.write("SCREEN_DUMP")
        screen_image_png = self.scope.read_raw()
        with open(png_fn, "wb") as file:
            file.write(screen_image_png)
        if show_plot:
            import matplotlib.image as mpimg
            import matplotlib.pyplot as plt

            x = mpimg.imread(png_fn)
            h, w = np.shape(x)[:2]
            plt.figure(num=fig_name, figsize=(w / 100, h / 100), dpi=100, facecolor="w", edgecolor="k")
            plt.subplots_adjust(left=0.0, right=1.0, bottom=0.0, top=1.0)
            plt.imshow(x)
        return png_fn

    def write_status_msg(self, msg):
        if len(msg) > 49:
            self.scope.write('MESSAGE "' + msg[0:46] + '..."')
        else:
            self.scope.write('MESSAGE "' + msg + '"')

    def validate_channel(self, Cn) -> str:
        # Bound by the detected channel count (4 or 8). channel_names and
        # n_channels have class-level 4-channel defaults, so this works even on a
        # test fake built via __new__ that skips __init__.
        if type(Cn) == str and Cn in self.channel_names:
            return Cn
        if type(Cn) == int and 1 <= Cn <= self.n_channels:
            return "C" + str(Cn)
        raise RuntimeError(f'**** validate_channel(): channel = "{Cn}" is not allowed, must be C1-{self.n_channels}')

    def validate_trace(self, tr) -> str:
        if type(tr) == int and 1 <= tr <= self.n_channels:
            return "C" + str(tr)
        for trn in self.valid_trace_names:
            if tr == trn:
                return trn
        raise RuntimeError(f'**** validate_trace(): trace name "{tr}" is unknown').with_traceback(sys.exc_info()[2])

    def max_samples(self, N=0) -> int:
        if N > 0:
            self.scope.write('VBS "app.Acquisition.Horizontal.MaxSamples=' + str(N) + '"')
        return _vbs_int(self.scope.query('VBS? "return=app.Acquisition.Horizontal.NumPoints"'), 0)

    def _scan_displayed(self, names) -> Tuple[str, ...]:
        """Return the subset of ``names`` whose :TRACE? reports ``ON``.

        A garbled or timed-out reply for one name is skipped (treated as not
        displayed) rather than aborting the whole scan: this scan backs
        ``_resolve_ref_channel`` and thus every arm helper, so one transient bad
        reply must not take down a run. A genuine connection drop
        (``ScopeConnectionError``) still propagates -- there is nothing left to
        scan once the link is gone.
        """
        self.scope.write("COMM_HEADER OFF")
        displayed = ()
        for name in names:
            try:
                if self.scope.query(name + ":TRACE?")[0:2] == "ON":
                    displayed += (name,)
            except ScopeConnectionError:
                raise
            except Exception:
                continue
        return displayed

    def displayed_channels(self) -> Tuple[str, ...]:
        # Scan only the channels this scope actually has (C1-C4 or C1-C8, from
        # the init-time detection) so we never query non-existent channels --
        # which on a 4-channel scope would stall up to the socket timeout.
        return self._scan_displayed(self.channel_names)

    def displayed_traces(self):
        return self._scan_displayed(self.valid_trace_names)

    def vertical_scale(self, trace) -> float:
        Tn = self.validate_trace(trace)
        return _vbs_float(self.scope.query('VBS? "Return=app.Acquisition.' + Tn + '.VerScale"'), 0.0)

    def set_vertical_scale(self, trace, scale) -> float:
        Tn = self.validate_trace(trace)
        self.scope.write('VBS "app.Acquisition.' + Tn + '.VerScaleVariable=True"')
        self.scope.write('VBS "app.Acquisition.' + Tn + ".VerScale=" + str(scale) + '"')
        return self.vertical_scale(trace)

    def averaging_count(self, channel="C1") -> int:
        Cn = self.validate_channel(channel)
        return _vbs_int(self.scope.query('VBS? "Return=app.Acquisition.' + Cn + '.AverageSweeps"'), 1)

    def set_averaging_count(self, channel="C1", NSweeps=1):
        Cn = self.validate_channel(channel)
        NSweeps = min(max(NSweeps, 1), 1000000)
        self.scope.write('VBS "app.Acquisition.' + Cn + ".AverageSweeps=" + str(NSweeps) + '"')

    def max_averaging_count(self) -> Tuple[int, str]:
        NSweeps = 0
        ach = None
        for ch in self.displayed_channels():
            n = self.averaging_count(ch)
            if n > NSweeps:
                NSweeps = n
                ach = ch
        if ach is None:
            raise RuntimeError("**** max_averaging_count(): no displayed channels").with_traceback(sys.exc_info()[2])
        return NSweeps, ach

    def wait_for_max_sweeps(self, aux_text="", timeout=100):
        NSweeps, ach = self.max_averaging_count()
        self.write_status_msg(aux_text + "Waiting for averaging(" + str(NSweeps) + ") to complete")
        if NSweeps == 1:
            self.scope.write("CLEAR_SWEEPS")
            time.sleep(0.05)
            print("      Starting single sweep acquisition...", end="", flush=True)
            self.set_trigger_mode("SINGLE")
            sweep_start_time = time.time()
            scope_stopped = False
            while time.time() - sweep_start_time < timeout:
                time.sleep(0.01)
                current_mode = self.set_trigger_mode("")
                if current_mode[0:4] == "STOP":
                    scope_stopped = True
                    break
            print(" Complete!" if scope_stopped else " Timed out!")
            timed_out, n = (False, 1) if scope_stopped else (True, 0)
        else:
            timed_out, n = self.wait_for_sweeps(ach, NSweeps, timeout)
        msg = "averaging timed out at:" + str(n) + "/" + str(NSweeps) + "after %.1f s" % timeout if timed_out else "averaging(" + str(NSweeps) + "), completed, got " + str(n)
        self.write_status_msg(aux_text + msg)
        return timed_out, n

    def wait_for_sweeps(self, channel, NSweeps, timeout=100, sleep_interval=0.1):
        channel = self.validate_channel(channel)
        self.scope.write(channel + ":WAVEFORM?")
        trace_bytes = self.scope.read_raw()
        initial_sweeps_per_acq = struct.unpack("=l", trace_bytes[SWEEPS_PER_ACQ_OFFSET:SWEEPS_PER_ACQ_OFFSET + 4])[0]
        self.set_trigger_mode("AUTO")
        self.scope.write("CLEAR_SWEEPS")
        time.sleep(0.25)
        self.set_trigger_mode("NORM")
        sweeps_per_acq = struct.unpack("=l", trace_bytes[SWEEPS_PER_ACQ_OFFSET:SWEEPS_PER_ACQ_OFFSET + 4])[0]
        clear_sweeps_timeout = time.time() + 10
        while time.time() < clear_sweeps_timeout and sweeps_per_acq > 1:
            self.scope.write(channel + ":WAVEFORM?")
            trace_bytes = self.scope.read_raw()
            sweeps_per_acq = struct.unpack("=l", trace_bytes[SWEEPS_PER_ACQ_OFFSET:SWEEPS_PER_ACQ_OFFSET + 4])[0]
            if sweeps_per_acq < initial_sweeps_per_acq or sweeps_per_acq == 1:
                break
        self.scope.write("COMM_FORMAT DEF9,BYTE,BIN")
        self.scope.write("WAVEFORM_SETUP SP,0,NP,1,FP,1,SN,0")
        self.scope.write("COMM_HEADER OFF")
        deadline = time.time() + timeout
        print(f"      Waiting for {NSweeps} sweeps: 0/{NSweeps}", end="", flush=True)
        timed_out = True
        gaaak = 0
        while time.time() < deadline:
            time.sleep(sleep_interval)
            self.scope.write(channel + ":WAVEFORM?")
            trace_bytes = self.scope.read_raw()
            sweeps_per_acq = struct.unpack("=l", trace_bytes[SWEEPS_PER_ACQ_OFFSET:SWEEPS_PER_ACQ_OFFSET + 4])[0]
            gaaak = sweeps_per_acq
            if sweeps_per_acq >= NSweeps:
                timed_out = False
                break
            print(f"\r      Waiting for {NSweeps} sweeps: {sweeps_per_acq}/{NSweeps}", end="", flush=True)
        self.set_trigger_mode("STOP")
        self.scope.write(channel + ":WAVEFORM?")
        trace_bytes = self.scope.read_raw()
        sweeps_per_acq = struct.unpack("=l", trace_bytes[SWEEPS_PER_ACQ_OFFSET:SWEEPS_PER_ACQ_OFFSET + 4])[0]
        if gaaak > sweeps_per_acq:
            self.gaaak_count += 1
            return self.wait_for_sweeps(channel, NSweeps, timeout, sleep_interval)
        print(f"\r      Waiting for {NSweeps} sweeps: {sweeps_per_acq}/{NSweeps} - Complete!")
        return timed_out, sweeps_per_acq

    def translate_wavedesc_bytes(self, wavedesc_bytes) -> WAVEDESC:
        return WAVEDESC._make(struct.unpack(WAVEDESC_FMT, wavedesc_bytes))

    def parse_wavedesc(self, wd):
        if wd.comm_type not in [0, 1]:
            raise RuntimeError(f"**** wd.comm_type = {wd.comm_type}; expected value is either 0 or 1").with_traceback(sys.exc_info()[2])
        # Sequence-mode branch disabled (see module TODO):
        # is_sequence = wd.subarray_count > 1
        # if is_sequence:
        #     NSamples = int(wd.wave_array_1 / wd.subarray_count)
        #     if wd.comm_type == 1:
        #         NSamples = int(NSamples / 2)
        # else:
        #     NSamples = wd.wave_array_1 if wd.comm_type == 0 else int(wd.wave_array_1 / 2)
        NSamples = wd.wave_array_1 if wd.comm_type == 0 else int(wd.wave_array_1 / 2)
        if NSamples == 0:
            raise RuntimeError("**** fail because NSamples = 0 (possible cause: trace has no data? scope not triggered?)").with_traceback(sys.exc_info()[2])
        if self.verbose:
            print("<:> record type:      ", RECORD_TYPES[wd.record_type])
            print("<:> timebase:         ", TIMEBASE_IDS[wd.timebase], "per div")
            print("<:> vertical gain:    ", VERT_GAIN_IDS[wd.fixed_vert_gain], "per div")
            print("<:> vertical coupling:", VERT_COUPLINGS[wd.vert_coupling])
            print("<:> processing:       ", PROCESSING_TYPES[wd.processing_done])
        ndx0 = (15 + WAVEDESC_SIZE) + wd.user_text + wd.trigtime_array + wd.ris_time_array + wd.res_array1
        return NSamples, ndx0

    def acquire_bytes(self, trace, seg=0):
        trace = self.validate_trace(trace)
        self.scope.write(f"WAVEFORM_SETUP SP,0,NP,0,FP,1,SN,{seg}")
        if self.verbose:
            print("\n<:> reading", trace, "from scope")
        self.scope.write(trace + ":WAVEFORM?")
        trace_bytes = self.scope.read_raw()
        if len(trace_bytes) < 15 + WAVEDESC_SIZE:
            raise LeCroyNoDataError(
                f"{trace}: :WAVEFORM? returned {len(trace_bytes)} bytes "
                f"(need >= {15 + WAVEDESC_SIZE}); trace likely has no data"
            )
        wavedesc_bytes = trace_bytes[15:15 + WAVEDESC_SIZE]
        self.trace_bytes = trace_bytes
        return trace_bytes, wavedesc_bytes

    def acquire(self, trace, seg=0, raw=False):
        trace_bytes, wavedesc_bytes = self.acquire_bytes(trace, seg)
        wd = self.translate_wavedesc_bytes(wavedesc_bytes)
        self.wd = wd
        NSamples, ndx0 = self.parse_wavedesc(wd)
        data = self._parse_wave_array(trace_bytes, wd, NSamples, ndx0, raw=raw)
        return data, wavedesc_bytes

    def _parse_wave_array(self, trace_bytes, wd, NSamples, ndx0, raw=False):
        if wd.comm_type == 1:
            data = np.frombuffer(trace_bytes, dtype="<i2", count=NSamples, offset=ndx0)
        else:
            data = np.frombuffer(trace_bytes, dtype=np.int8, count=NSamples, offset=ndx0)
        if raw:
            return data
        return data.astype(np.float64, copy=False) * wd.vertical_gain - wd.vertical_offset

    # Sequence-mode acquisition disabled (see module TODO); reference impl:
    # def acquire_sequence_data(self, trace):
    #     _trace_bytes, wavedesc_bytes = self.acquire_bytes(trace)
    #     wd = self.translate_wavedesc_bytes(wavedesc_bytes)
    #     if wd.subarray_count < 2:
    #         raise RuntimeError("Sequence mode requires at least 2 segments.")
    #     segment_data = []
    #     for segment in range(1, wd.subarray_count + 1):
    #         data, _ = self.acquire(trace, segment)
    #         segment_data.append(data)
    #     return segment_data, wavedesc_bytes
    def acquire_sequence_data(self, trace):
        raise NotImplementedError(
            "acquire_sequence_data is disabled in this version; sequence-mode "
            "acquisition does not work and is pending a fix."
        )

    def time_array(self, trace=None):
        if trace is None and hasattr(self, "wd"):
            wd = self.wd
        else:
            _trace_bytes, wavedesc_bytes = self.acquire_bytes(trace)
            wd = self.translate_wavedesc_bytes(wavedesc_bytes)
        # Sequence-mode branch disabled (see module TODO):
        # if wd.subarray_count > 1:
        #     NSamples = int(wd.wave_array_1 / wd.subarray_count)
        #     if wd.comm_type == 1:
        #         NSamples = int(NSamples / 2)
        # else:
        #     NSamples = wd.wave_array_1 if wd.comm_type == 0 else int(wd.wave_array_1 / 2)
        NSamples = wd.wave_array_1 if wd.comm_type == 0 else int(wd.wave_array_1 / 2)
        t0 = float(wd.horiz_offset)
        horiz_interval = float(wd.horiz_interval)
        return np.linspace(t0, t0 + NSamples * horiz_interval, NSamples, endpoint=False)

    def get_sequence_trigger_times(self):
        raise RuntimeError("get_sequence_trigger_times is experimental and not implemented in lab_scopes yet.")

    def set_trigger_mode(self, trigger_mode) -> str:
        self.scope.write("COMM_HEADER OFF")
        prev_trigger_mode = self.scope.query("TRIG_MODE?")
        if trigger_mode == "AUTO":
            self.scope.write("TRIG_MODE AUTO")
        elif trigger_mode == "NORM":
            self.scope.write("TRIG_MODE NORM")
        elif trigger_mode == "SINGLE":
            self.scope.write("TRIG_MODE SINGLE")
        elif trigger_mode == "STOP":
            self.scope.write("TRIG_MODE STOP")
        else:
            return prev_trigger_mode
        for i in range(25):
            txt = self.scope.query("TRIG_MODE?")
            if txt[0:3] == trigger_mode[0:3]:
                break
            # Fast-trigger case: when arming SINGLE against a free-running timer,
            # an edge can arrive and the sweep complete before we read back
            # "SIN", so the scope already reports "STOP". That is a successful
            # arm-then-immediate-capture, not a failed arm -- accept it instead
            # of burning all 25 retries waiting for a "SIN" that is already gone.
            # (The master path, arm_master_single, makes its own strict SIN check
            # afterward, so it does not depend on this shortcut.)
            if trigger_mode == "SINGLE" and txt[0:3] == "STO":
                break
            print("set_trigger_mode(", trigger_mode, ")    attempt", i, ":  TRIG_MODE is", txt)
            time.sleep(0.1)
        return prev_trigger_mode

    def clear_sweeps(self) -> None:
        """Reset the scope's sweep/acquisition counter.

        After this, ``sweeps_per_acq`` reads as the count of acquisitions
        completed *since the clear*, so the next completed sweep is detectable
        as a fresh capture rather than a leftover STOP from a previous shot.
        """
        self.scope.write("CLEAR_SWEEPS")

    def _read_sweeps_per_acq(self, channel) -> int:
        """Read the sweep counter, assuming WAVEFORM_SETUP is already minimal.

        Hot-path helper: skips the WAVEFORM_SETUP write so a tight poll loop
        (wait_for_stop_then_complete) issues one write + one read per iteration
        instead of two writes. The counter lives in the fixed 346-byte WAVEDESC
        header, returned in full regardless of the data-point count.
        """
        self.scope.write(channel + ":WAVEFORM?")
        trace_bytes = self.scope.read_raw()
        return struct.unpack(
            "=l", trace_bytes[SWEEPS_PER_ACQ_OFFSET:SWEEPS_PER_ACQ_OFFSET + 4]
        )[0]

    def sweeps_per_acq(self, channel) -> int:
        """Return the completed-sweep counter from the channel's WAVEDESC.

        This is a monotonic count (within a shot, after ``clear_sweeps``) of how
        many acquisitions the scope has completed. It distinguishes a freshly
        captured trigger (counter advanced) from an ambiguous STOP state, which
        the scope reports both before any trigger and after a previous one.

        The counter lives in the fixed 346-byte WAVEDESC header, which is always
        returned in full, so we request a single data point (NP,1) to keep this
        read cheap -- otherwise it would download the whole waveform.
        """
        channel = self.validate_channel(channel)
        self.scope.write("WAVEFORM_SETUP SP,0,NP,1,FP,1,SN,0")
        return self._read_sweeps_per_acq(channel)

    def _resolve_ref_channel(self, channel=None) -> str:
        """Return the reference channel to poll: ``channel`` or the first displayed.

        Shared by the arm helpers so the channel-discovery fallback (a scan of
        the detected input channels via :TRACE? queries) lives in one place.
        """
        if channel is None:
            channels = self.displayed_channels()
            if not channels:
                raise RuntimeError("**** no displayed channels to poll")
            return channels[0]
        return self.validate_channel(channel)

    def arm_single(self, channel=None) -> str:
        """Arm the scope for a single trigger and return the reference channel.

        Clears the sweep counter first so a subsequent ``wait_for_stop_then_complete``
        on the returned channel sees an unambiguous 0 -> >=1 transition when a
        fresh trigger lands. The reference channel is ``channel`` if given, else
        the first displayed channel.
        """
        channel = self._resolve_ref_channel(channel)
        self.clear_sweeps()
        self.set_trigger_mode("SINGLE")
        return channel

    def read_inr(self) -> int:
        """Return the LeCroy INR (Internal state change Register) as an int.

        Returns 0 on a non-integer/blank response rather than raising, so a
        transient communication blip cannot abort a run. Note INR is
        read-to-clear: each read returns the bits set *since the last read*.

        The last whitespace-separated token is parsed, so this is robust whether
        COMM_HEADER is OFF (bare ``8192``) or ON (``INR 8192``).
        """
        resp = self.scope.query("INR?")
        token = resp.split()[-1] if isinstance(resp, str) and resp.split() else resp
        return _vbs_int(token, 0)

    def wait_for_trigger_ready(self, timeout=5.0, poll=0.01) -> bool:
        """Block until the scope reports its trigger is armed and waiting.

        Polls the INR register for the ``INR_TRIGGER_READY`` bit and returns True
        as soon as it is seen, or False on timeout. Because INR is read-to-clear,
        the bit is OR-accumulated across reads within the wait so a set-then-clear
        transition between two polls is not lost.

        Used to confirm a slave scope is actually listening on its EXT input
        before the master is armed -- the master's trigger-out drives the slaves,
        so a slave that is not yet ready would miss the master's edge and desync.
        """
        seen = 0
        deadline = time.time() + timeout
        while time.time() < deadline:
            seen |= self.read_inr()
            if seen & INR_TRIGGER_READY:
                return True
            time.sleep(poll)
        return False

    def arm_single_and_confirm(self, channel=None, ready_timeout=5.0):
        """Arm for a single trigger and confirm the hardware is trigger-ready.

        Returns ``(channel, ready)`` where ``channel`` is the reference channel
        (as from ``arm_single``) and ``ready`` is True iff the INR trigger-ready
        bit was observed within ``ready_timeout``. Slaves use this so the caller
        can withhold arming the master until every slave is confirmed ready.
        """
        channel = self.arm_single(channel=channel)
        ready = self.wait_for_trigger_ready(timeout=ready_timeout)
        return channel, ready

    def arm_master_single(self, channel=None) -> str:
        """Arm the master scope for a single trigger (exactly once).

        The master is armed with a single ``CLEAR_SWEEPS`` + ``TRIG_MODE SINGLE``
        and is NOT re-armed if it does not read back ``SIN``. Re-arming would
        re-pulse the master's trigger-out, which drives the slaves' EXT input, and
        a stray pulse around the shot boundary can double-trigger a slave (the
        slave's front panel shows SINGLE twice). Arming once removes that cause;
        correctness for the shot is still guaranteed by the per-shot completion
        check (sweep counter; see ``wait_for_stop_then_complete``).

        If the master reads ``STOP`` instead of ``SIN`` (it fired before we could
        read back, possible only when edges arrive faster than a round-trip), a
        warning is printed but the shot proceeds best-effort. Returns the
        reference channel.
        """
        channel = self._resolve_ref_channel(channel)
        self.clear_sweeps()
        self.set_trigger_mode("SINGLE")
        # Both SIN (armed, waiting for an edge) and STO (armed then fired before
        # this readback round-trip -- common against a fast free-running timer)
        # are healthy outcomes; set_trigger_mode already accepts STO as a valid
        # arm. Only a genuinely unexpected mode (AUTO/NORM/blank) means the arm
        # did not take, so warn solely on that to avoid a per-shot false alarm.
        mode = self.scope.query("TRIG_MODE?")[0:3]
        if mode not in ("SIN", "STO"):
            print("**** arm_master_single(): master did not arm to SINGLE "
                  f"(TRIG_MODE reads {mode!r}); proceeding best-effort.")
        return channel

    def wait_for_stop_then_complete(self, channel, timeout=100, poll=0.02) -> bool:
        """Block until the scope is STOPped AND a fresh sweep is confirmed.

        Two-stage completion check: first wait for ``TRIG_MODE?`` to read ``STOP``
        (a cheap, fast hint that the single acquisition has ended), then confirm
        via the sweep counter that a fresh sweep actually landed this shot
        (``sweeps_per_acq`` >= 1 after the arm-time ``clear_sweeps``). The counter
        is the source of truth -- STOP alone is ambiguous (the scope reads STOP
        both before any trigger and after a previous one), so a leftover STOP from
        a prior shot, which reads counter 0, is never mistaken for fresh data.

        Returns True once both hold, False on timeout. WAVEFORM_SETUP is made
        minimal once up front so the counter reads stay cheap.
        """
        channel = self.validate_channel(channel)
        self.scope.write("WAVEFORM_SETUP SP,0,NP,1,FP,1,SN,0")
        deadline = time.time() + timeout
        while time.time() < deadline:
            # Stage 1: STOP hint (cheap TRIG_MODE? read).
            if self.scope.query("TRIG_MODE?")[0:3] == "STO":
                # Stage 2: confirm a fresh sweep actually landed (authoritative).
                if self._read_sweeps_per_acq(channel) >= 1:
                    return True
            time.sleep(poll)
        return False

    def expanded_name(self, tr) -> str:
        return EXPANDED_TRACE_NAMES.get(tr, "unknown_trace_name")

    def dumtest(self):
        r1 = self.scope.query("PANEL_SETUP?")
        self.scope.write("*SAV 1")
        print(len(r1))
        self.scope.write('VBS app.SaveRecall.Setup.PanelFilename="REMOTE"')
        r2 = self.scope.query("app.SaveRecall.Setup.DoSavePanel")
        print(len(r2))

    def autoscale(self, trace):
        raise RuntimeError("autoscale is not yet ported to the no-PyVISA driver.")

    def calibrate(self, a=True):
        if a:
            prev_timeout = self.scope.timeout
            self.scope.timeout = max(prev_timeout, 60.0)
            try:
                self.scope.query("*CAL?")
            finally:
                self.scope.timeout = prev_timeout
        else:
            self.scope.write("AUTO_CALIBRATE OFF")

    # Deprecated method aliases retained for one release. Prefer the *_wavedesc
    # names; the WAVEDESC is what these methods actually operate on, not the
    # 8-byte VICP frame header nor the IEEE 488.2 TMC block prefix.
    translate_header_bytes = translate_wavedesc_bytes
    parse_header = parse_wavedesc


class Fake_Scope:
    """Small compatibility fake used by older LAPD_DAQ scripts."""

    def __init__(self, idn_string="Fake LeCroy", max_samples=0, traces=(), displayed_traces=()):
        self.idn_string = idn_string
        self._max_samples = max_samples
        self._traces = tuple(traces)
        self._displayed_traces = tuple(displayed_traces or traces)

    def max_samples(self, N=0):
        return self._max_samples if N == 0 else N

    def displayed_traces(self):
        return self._displayed_traces

    def expanded_name(self, tr):
        return EXPANDED_TRACE_NAMES.get(tr, "unknown_trace_name")


def _seconds(timeout):
    value = float(timeout)
    return value / 1000.0 if value > 100 else value


LeCroy_Scope = LeCroyScope

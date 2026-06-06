"""LeCroy oscilloscope driver without PyVISA.

This is based on the LAPD_DAQ ``LeCroy_Scope.py`` driver surface, but the
connection is a native VICP transport instead of VISA.

TODO: Sequence-mode acquisition is broken in this version. The
``acquire_sequence_data`` method and the sequence branches in
``parse_wavedesc`` / ``time_array`` are commented out below until the
segment read path is fixed. See also tests/test_lecroy_scope_acquire.py
and tests/test_lecroy_scope_real.py for the corresponding disabled tests.
"""

from __future__ import annotations

import struct
import sys
import time
from typing import Tuple

import numpy as np

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

    def __init__(self, ipv4_addr: str, verbose: bool = True, timeout: float = 5.0,
                 port: int = 1861, transport=None):
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

        if len(self.valid_trace_names) == 0:
            for tr in KNOWN_TRACE_NAMES:
                try:
                    self.scope.query(tr + ":TRACE?")
                    error_code = int(self.scope.query("CMR?"))
                except Exception:
                    continue
                if error_code == 0:
                    self.valid_trace_names += (tr,)

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
        if type(Cn) == str and Cn in ("C1", "C2", "C3", "C4"):
            return Cn
        if type(Cn) == int and 1 <= Cn <= 4:
            return "C" + str(Cn)
        raise RuntimeError(f'**** validate_channel(): channel = "{Cn}" is not allowed, must be C1-4').with_traceback(sys.exc_info()[2])

    def validate_trace(self, tr) -> str:
        if type(tr) == int and 1 <= tr <= 4:
            return "C" + str(tr)
        for trn in self.valid_trace_names:
            if tr == trn:
                return trn
        raise RuntimeError(f'**** validate_trace(): trace name "{tr}" is unknown').with_traceback(sys.exc_info()[2])

    def max_samples(self, N=0) -> int:
        if N > 0:
            self.scope.write('VBS "app.Acquisition.Horizontal.MaxSamples=' + str(N) + '"')
        return _vbs_int(self.scope.query('VBS? "return=app.Acquisition.Horizontal.NumPoints"'), 0)

    def displayed_channels(self) -> Tuple[str, ...]:
        channels = ()
        self.scope.write("COMM_HEADER OFF")
        for ch in ("C1", "C2", "C3", "C4"):
            if self.scope.query(ch + ":TRACE?")[0:2] == "ON":
                channels += (ch,)
        return channels

    def displayed_traces(self):
        traces = ()
        self.scope.write("COMM_HEADER OFF")
        for tr in self.valid_trace_names:
            if self.scope.query(tr + ":TRACE?")[0:2] == "ON":
                traces += (tr,)
        return traces

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
        # TODO: sequence-mode disabled — restore the is_sequence branch when fixed.
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

    # TODO: sequence-mode acquisition is broken — re-enable once the per-segment
    # :WAVEFORM? read path is fixed and matches LAPD_DAQ behavior.
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
        # TODO: sequence-mode disabled — restore the subarray_count > 1 branch when fixed.
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
        (wait_for_single_complete) issues one write + one read per iteration
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
        C1..C4 :TRACE? queries) lives in one place.
        """
        if channel is None:
            channels = self.displayed_channels()
            if not channels:
                raise RuntimeError(
                    "**** no displayed channels to poll"
                ).with_traceback(sys.exc_info()[2])
            return channels[0]
        return self.validate_channel(channel)

    def arm_single(self, channel=None) -> str:
        """Arm the scope for a single trigger and return the reference channel.

        Clears the sweep counter first so a subsequent ``wait_for_single_complete``
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
        check (sweep counter; see ``wait_for_single_complete`` /
        ``wait_for_stop_then_complete``).

        If the master reads ``STOP`` instead of ``SIN`` (it fired before we could
        read back, possible only when edges arrive faster than a round-trip), a
        warning is printed but the shot proceeds best-effort. Returns the
        reference channel.
        """
        channel = self._resolve_ref_channel(channel)
        self.clear_sweeps()
        self.set_trigger_mode("SINGLE")
        if self.scope.query("TRIG_MODE?")[0:3] != "SIN":
            print("**** arm_master_single(): master did not hold SIN after arming "
                  "(it may have fired before readback); proceeding best-effort.")
        return channel

    def wait_for_single_complete(self, channel, timeout=100, poll=0.02) -> bool:
        """Block until a fresh single acquisition has completed.

        Uses the sweep counter as the source of truth: returns ``True`` as soon
        as the counter is >= 1 (a fresh sweep landed after the ``clear_sweeps``
        done in ``arm_single``). Because the counter was cleared at arm time, a
        leftover STOP from a previous shot reads as 0 and cannot be mistaken for
        a new acquisition. Returns ``False`` on timeout.

        WAVEFORM_SETUP is made minimal once up front, then each poll issues a
        single :WAVEFORM? read, so a tight wait does not re-send the setup or
        download full waveforms.
        """
        channel = self.validate_channel(channel)
        self.scope.write("WAVEFORM_SETUP SP,0,NP,1,FP,1,SN,0")
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._read_sweeps_per_acq(channel) >= 1:
                return True
            time.sleep(poll)
        return False

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

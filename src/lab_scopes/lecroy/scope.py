"""LeCroy oscilloscope driver without PyVISA.

This is based on the LAPD_DAQ ``LeCroy_Scope.py`` driver surface, but the
connection is a native VICP transport instead of VISA.
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
        if type(Cn) == str and Cn in ("C1", "C2", "C3", "C4", "C5", "C6", "C7", "C8"):
            return Cn
        if type(Cn) == int and 1 <= Cn <= 8:
            return "C" + str(Cn)
        raise RuntimeError(f'**** validate_channel(): channel = "{Cn}" is not allowed, must be C1-8').with_traceback(sys.exc_info()[2])

    def validate_trace(self, tr) -> str:
        if type(tr) == int and 1 <= tr <= 8:
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
        hdr_bytes = self.scope.read_raw()
        initial_sweeps_per_acq = struct.unpack("=l", hdr_bytes[15 + 148:15 + 148 + 4])[0]
        self.set_trigger_mode("AUTO")
        self.scope.write("CLEAR_SWEEPS")
        time.sleep(0.25)
        self.set_trigger_mode("NORM")
        sweeps_per_acq = struct.unpack("=l", hdr_bytes[15 + 148:15 + 148 + 4])[0]
        clear_sweeps_timeout = time.time() + 10
        while time.time() < clear_sweeps_timeout and sweeps_per_acq > 1:
            self.scope.write(channel + ":WAVEFORM?")
            hdr_bytes = self.scope.read_raw()
            sweeps_per_acq = struct.unpack("=l", hdr_bytes[15 + 148:15 + 148 + 4])[0]
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
            hdr_bytes = self.scope.read_raw()
            sweeps_per_acq = struct.unpack("=l", hdr_bytes[15 + 148:15 + 148 + 4])[0]
            gaaak = sweeps_per_acq
            if sweeps_per_acq >= NSweeps:
                timed_out = False
                break
            print(f"\r      Waiting for {NSweeps} sweeps: {sweeps_per_acq}/{NSweeps}", end="", flush=True)
        self.set_trigger_mode("STOP")
        self.scope.write(channel + ":WAVEFORM?")
        hdr_bytes = self.scope.read_raw()
        sweeps_per_acq = struct.unpack("=l", hdr_bytes[15 + 148:15 + 148 + 4])[0]
        if gaaak > sweeps_per_acq:
            self.gaaak_count += 1
            return self.wait_for_sweeps(channel, NSweeps, timeout, sleep_interval)
        print(f"\r      Waiting for {NSweeps} sweeps: {sweeps_per_acq}/{NSweeps} - Complete!")
        return timed_out, sweeps_per_acq

    def translate_header_bytes(self, header_bytes) -> WAVEDESC:
        return WAVEDESC._make(struct.unpack(WAVEDESC_FMT, header_bytes))

    def parse_header(self, hdr):
        if hdr.comm_type not in [0, 1]:
            raise RuntimeError(f"**** hdr.comm_type = {hdr.comm_type}; expected value is either 0 or 1").with_traceback(sys.exc_info()[2])
        is_sequence = hdr.subarray_count > 1
        if is_sequence:
            NSamples = int(hdr.wave_array_1 / hdr.subarray_count)
            if hdr.comm_type == 1:
                NSamples = int(NSamples / 2)
        else:
            NSamples = hdr.wave_array_1 if hdr.comm_type == 0 else int(hdr.wave_array_1 / 2)
        if NSamples == 0:
            raise RuntimeError("**** fail because NSamples = 0 (possible cause: trace has no data? scope not triggered?)").with_traceback(sys.exc_info()[2])
        if self.verbose:
            print("<:> record type:      ", RECORD_TYPES[hdr.record_type])
            print("<:> timebase:         ", TIMEBASE_IDS[hdr.timebase], "per div")
            print("<:> vertical gain:    ", VERT_GAIN_IDS[hdr.fixed_vert_gain], "per div")
            print("<:> vertical coupling:", VERT_COUPLINGS[hdr.vert_coupling])
            print("<:> processing:       ", PROCESSING_TYPES[hdr.processing_done])
        ndx0 = (15 + WAVEDESC_SIZE) + hdr.user_text + hdr.trigtime_array + hdr.ris_time_array + hdr.res_array1
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
        header_bytes = trace_bytes[15:15 + WAVEDESC_SIZE]
        self.trace_bytes = trace_bytes
        return trace_bytes, header_bytes

    def acquire(self, trace, seg=0, raw=False):
        trace_bytes, header_bytes = self.acquire_bytes(trace, seg)
        hdr = self.translate_header_bytes(header_bytes)
        self.hdr = hdr
        NSamples, ndx0 = self.parse_header(hdr)
        data = self._parse_wave_array(trace_bytes, hdr, NSamples, ndx0, raw=raw)
        return data, header_bytes

    def _parse_wave_array(self, trace_bytes, hdr, NSamples, ndx0, raw=False):
        if hdr.comm_type == 1:
            data = np.frombuffer(trace_bytes, dtype="<i2", count=NSamples, offset=ndx0)
        else:
            data = np.frombuffer(trace_bytes, dtype=np.int8, count=NSamples, offset=ndx0)
        if raw:
            return data
        return data.astype(np.float64, copy=False) * hdr.vertical_gain - hdr.vertical_offset

    def acquire_sequence_data(self, trace):
        trace_bytes, header_bytes = self.acquire_bytes(trace, seg=0)
        hdr = self.translate_header_bytes(header_bytes)
        if hdr.subarray_count < 2:
            raise RuntimeError("Sequence mode requires at least 2 segments.")
        self.hdr = hdr
        NSamples, ndx0 = self.parse_header(hdr)
        total_samples = NSamples * hdr.subarray_count
        data = self._parse_wave_array(trace_bytes, hdr, total_samples, ndx0, raw=False)
        segment_data = list(data.reshape(hdr.subarray_count, NSamples))
        return segment_data, header_bytes

    def time_array(self, trace=None):
        if trace is None and hasattr(self, "hdr"):
            hdr = self.hdr
        else:
            _trace_bytes, header_bytes = self.acquire_bytes(trace)
            hdr = self.translate_header_bytes(header_bytes)
        if hdr.subarray_count > 1:
            NSamples = int(hdr.wave_array_1 / hdr.subarray_count)
            if hdr.comm_type == 1:
                NSamples = int(NSamples / 2)
        else:
            NSamples = hdr.wave_array_1 if hdr.comm_type == 0 else int(hdr.wave_array_1 / 2)
        t0 = float(hdr.horiz_offset)
        horiz_interval = float(hdr.horiz_interval)
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
            print("set_trigger_mode(", trigger_mode, ")    attempt", i, ":  TRIG_MODE is", txt)
            time.sleep(0.1)
        return prev_trigger_mode

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

"""Real-hardware shot timing for RigolDHO800: before PR #4, PR #4, PR #4 + TCP_NODELAY.

Skipped unless RIGOL_SCOPE_IP is set. Run on its own, after test_rigol_scope_real.py:

    RIGOL_SCOPE_IP=<address> pytest tests/test_rigol_timing_real.py -v -s

Each shot is one connection, as interf_raw.rigol_read does it: connect, wait for a real
trigger (:SINGle, excluded from the times), :STOP, read C1 and C2, :RUN, close. Shots are
paced by the scope's triggers, like normal acquisition; there is no rapid connect loop.
RIGOL_TIMING_SHOTS (default 3) shots per variant, so 3 x 3 = 9 connections.

Variants:
    old     two read_channel calls, six calibration queries, the 50 ms settle delays
            after :WAVeform:SOURce/MODE/FORMat put back (the v0.4.x command sequence)
    new     read_channels (this release)
    new+nd  read_channels with TCP_NODELAY on the socket (not in the driver yet; the
            LeCroy transport sets it to avoid stalls on back-to-back writes)
"""

import os
import socket
import statistics
import time

import pytest

import lab_scopes.rigol.dho800 as dho
from lab_scopes.rigol import RigolDHO800

SCOPE_IP = os.environ.get("RIGOL_SCOPE_IP", "")
SHOTS = int(os.environ.get("RIGOL_TIMING_SHOTS", "3"))
CHANNELS = ["C1", "C2"]
TRIGGER_TIMEOUT_S = 30.0
_WAVE_SETTINGS = (":WAVEFORM:SOURCE", ":WAVEFORM:MODE", ":WAVEFORM:FORMAT")

pytestmark = pytest.mark.skipif(not SCOPE_IP, reason="set RIGOL_SCOPE_IP to time a real Rigol scope")


def _with_old_settle_delays(command):
    def old_command(tn, scpi, *args, **kwargs):
        reply = command(tn, scpi, *args, **kwargs)
        if not scpi.endswith("?") and any(w in scpi.upper() for w in _WAVE_SETTINGS):
            time.sleep(0.05)
        return reply
    return old_command


def _shot(mode, monkeypatch):
    """Returns (seconds excluding the trigger wait, seconds for the C1+C2 read)."""
    with monkeypatch.context() as m:
        if mode == "old":
            m.setattr(dho, "command", _with_old_settle_delays(dho.command))
            m.setattr(RigolDHO800, "calibration_from_preamble", False)
        t0 = time.monotonic()
        s = RigolDHO800(SCOPE_IP, verbose=False)
        connect_s = time.monotonic() - t0
        try:
            if mode == "new+nd":
                s.tn.sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            s.single()
            s.wait_until_stopped(timeout=TRIGGER_TIMEOUT_S)
            t1 = time.monotonic()
            s.stop()
            if mode == "old":
                for ch in CHANNELS:
                    s.read_channel(ch)
            else:
                s.read_channels(CHANNELS)
            t2 = time.monotonic()
            s.run()
        finally:
            s.close()
        t3 = time.monotonic()
    return connect_s + (t3 - t1), t2 - t1


def test_shot_timing(monkeypatch, capsys):
    modes = ("old", "new", "new+nd")
    times = {m: [] for m in modes}
    for i in range(SHOTS):
        for mode in modes:
            total, read = _shot(mode, monkeypatch)
            times[mode].append((total, read))
            print(f"\nSHOT {i + 1} {mode}: total {total:.2f} s, read {read:.2f} s")
    for mode in modes:
        totals = [t for t, _ in times[mode]]
        reads = [r for _, r in times[mode]]
        print(f"\nMEDIAN {mode}: total {statistics.median(totals):.2f} s, "
              f"read {statistics.median(reads):.2f} s (min {min(totals):.2f}, max {max(totals):.2f})")
    assert statistics.median(r for _, r in times["new"]) < statistics.median(r for _, r in times["old"])

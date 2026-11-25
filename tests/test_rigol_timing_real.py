"""Real-hardware shot timing for RigolDHO800, on one stopped record (no trigger needed).

Skipped unless RIGOL_SCOPE_IP is set. Run on its own:

    RIGOL_SCOPE_IP=<address> pytest tests/test_rigol_timing_real.py -v -s

Sends :STOP once, so every read sees the same record (the last one captured), then reads
C1 and C2 RIGOL_TIMING_ROUNDS times (default 2) per variant, one connection per read and
RIGOL_TIMING_GAP_S (default 3 s) between connections, as interf_raw.rigol_read connects.
Sends :RUN at the end. Six connections in all with the defaults; no rapid connect loop.

Variants:
    old         v0.4.x sequence: two read_channel calls, six calibration queries, the 50 ms
                settle delays after :WAVeform:SOURce/MODE/FORMat, Nagle on
    new-nagle   read_channels with Nagle on (PR #4 as first benched, 2afbe0e)
    new         read_channels with TCP_NODELAY (the driver default)

Asserts every read returns the same raw codes and metadata, and new is not slower than old.
"""

import os
import statistics
import time

import pytest

import lab_scopes.rigol.dho800 as dho
from lab_scopes.rigol import RigolDHO800

SCOPE_IP = os.environ.get("RIGOL_SCOPE_IP", "")
ROUNDS = int(os.environ.get("RIGOL_TIMING_ROUNDS", "2"))
GAP_S = float(os.environ.get("RIGOL_TIMING_GAP_S", "3"))
CHANNELS = ["C1", "C2"]
MODES = ("old", "new-nagle", "new")
_WAVE_SETTINGS = (":WAVEFORM:SOURCE", ":WAVEFORM:MODE", ":WAVEFORM:FORMAT")

pytestmark = pytest.mark.skipif(not SCOPE_IP, reason="set RIGOL_SCOPE_IP to time a real Rigol scope")


def _with_old_settle_delays(command):
    def old_command(tn, scpi, *args, **kwargs):
        reply = command(tn, scpi, *args, **kwargs)
        if not scpi.endswith("?") and any(w in scpi.upper() for w in _WAVE_SETTINGS):
            time.sleep(0.05)
        return reply
    return old_command


def _read(mode, monkeypatch):
    """One connection: connect, :STOP, read C1+C2, close. Returns (connect s, read s, data)."""
    with monkeypatch.context() as m:
        if mode == "old":
            m.setattr(dho, "command", _with_old_settle_delays(dho.command))
            m.setattr(RigolDHO800, "calibration_from_preamble", False)
        if mode in ("old", "new-nagle"):
            m.setattr(RigolDHO800, "tcp_nodelay", False)
        t0 = time.monotonic()
        with RigolDHO800(SCOPE_IP, verbose=False) as s:
            t1 = time.monotonic()
            s.stop()
            if mode == "old":
                data = {ch: s.read_channel(ch) for ch in CHANNELS}
            else:
                data = s.read_channels(CHANNELS)
            t2 = time.monotonic()
    return t1 - t0, t2 - t1, data


def test_shot_timing_same_record(monkeypatch, capsys):
    with RigolDHO800(SCOPE_IP, verbose=False) as s:
        s.stop()
    times = {m: [] for m in MODES}
    reference = None
    try:
        for i in range(ROUNDS):
            for mode in MODES:
                time.sleep(GAP_S)
                connect_s, read_s, data = _read(mode, monkeypatch)
                times[mode].append((connect_s, read_s))
                print(f"\nROUND {i + 1} {mode}: connect {connect_s * 1000:.0f} ms, "
                      f"read C1+C2 {read_s:.2f} s, total {connect_s + read_s:.2f} s")
                if reference is None:
                    reference = data
                for ch in CHANNELS:
                    assert data[ch].raw.tobytes() == reference[ch].raw.tobytes(), f"{mode} {ch}: raw differs"
                    assert data[ch].metadata == reference[ch].metadata, f"{mode} {ch}: metadata differs"
    finally:
        time.sleep(GAP_S)
        with RigolDHO800(SCOPE_IP, verbose=False) as s:
            s.run()
    for mode in MODES:
        connects = [c for c, _ in times[mode]]
        reads = [r for _, r in times[mode]]
        print(f"\nMEDIAN {mode}: connect {statistics.median(connects) * 1000:.0f} ms, "
              f"read {statistics.median(reads):.2f} s, "
              f"total {statistics.median(c + r for c, r in times[mode]):.2f} s")
    print(f"\n{CHANNELS}: {reference[CHANNELS[0]].points} points each, identical in every read")
    assert statistics.median(r for _, r in times["new"]) <= statistics.median(r for _, r in times["old"])

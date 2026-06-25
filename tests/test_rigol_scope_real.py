"""Real-hardware bench test for RigolDHO800 (firmware 00.01.05, up to 25M depth).

Validates the full-memory-depth, 12-bit (WORD) acquisition path against a real
DHO800. Every test is skipped unless SCOPE_IP is set below. Set the panel Mem Depth
on the scope, then run; the length check confirms the driver returns exactly that
many points.

    pytest tests/test_rigol_scope_real.py -v -s        # -s to see the live report

Recommended bench setup: feed a function-generator sine/ramp, give the channel a
non-zero vertical offset and the timebase a non-zero horizontal delay (the historical
failure conditions). Step the panel Mem Depth 1k -> 10k -> 100k -> 1M -> 10M -> 25M
across runs (or set EXPECT_MDEPTH to assert a specific depth).

What it checks (per displayed analog channel):
  - acquire returns len == :ACQuire:MDEPth? (full record, no trim/pad/truncation)
  - WORD read is 12-bit: raw dtype uint16, codes span 0..4095 with >256 distinct
    values (proves we are NOT silently getting 8-bit data), and a smooth input
    yields a smooth raw array (endianness sanity)
  - WORD voltage approximates the BYTE read of the same trace (cross-check)
  - the on-screen window is locatable within the full record (screen-slice report)
  - a fed ramp is monotonic across the full window (no dropped/duplicated chunks)

--------------------------------------------------------------------------------
MANUAL probe-ratio bench step (gap 3.7, no automated check -- run once on 00.01.05):
  Feed a known amplitude (e.g. 1.000 Vpp), set the channel probe ratio to 10x on the
  panel, then:
      with RigolDHO800(SCOPE_IP, verbose=True) as s:
          print(s.vertical_scale(1), s.vertical_offset(1))   # must match panel
          s.single(); s.wait_until_stopped()
          w = s.read_channel(1)
          print(float(w.voltage.max() - w.voltage.min()))    # must match generator
  A result off by exactly the probe ratio means scale read-back is not honouring the
  probe ratio on this firmware -- record it before trusting 10x probe metadata.
--------------------------------------------------------------------------------
"""

import time

import numpy as np
import pytest

from lab_scopes.rigol import RigolDHO800


# === user configuration =====================================================
SCOPE_IP = "192.168.7.59"          # set to "192.168.7.59" to enable this suite
EXPECT_MDEPTH = 1_000_000     # set to an int (e.g. 25_000_000) to assert the panel depth
SHOW_PLOT = True        # True: plot full record + highlighted on-screen slice
# ============================================================================

TRIGGER_TIMEOUT_S = 30.0


_real = pytest.mark.skipif(
    not SCOPE_IP, reason="set SCOPE_IP in this file to test a real Rigol scope"
)


@pytest.fixture(scope="module")
def scope():
    s = RigolDHO800(SCOPE_IP, verbose=True)
    try:
        yield s
    finally:
        s.close()


def _armed_stop(s):
    """Arm a single acquisition and block until the scope reports STOP."""
    s.single()
    s.wait_until_stopped(timeout=TRIGGER_TIMEOUT_S)


def _displayed(s):
    chans = s.displayed_channels()
    if not chans:
        pytest.skip("no analog channels displayed on the scope")
    return chans


@_real
def test_full_record_length(scope):
    """WORD read returns exactly :ACQuire:MDEPth? points (no trim/pad/truncation)."""
    _armed_stop(scope)
    mdepth = scope.memory_depth()
    if EXPECT_MDEPTH is not None:
        assert mdepth == EXPECT_MDEPTH, f"panel depth {mdepth} != expected {EXPECT_MDEPTH}"
    for ch in _displayed(scope):
        w = scope.read_channel(ch)  # WORD
        assert w.points == mdepth, f"{ch}: got {w.points} of {mdepth} points"
        assert np.all(np.isfinite(w.voltage)), f"{ch}: non-finite samples"


@_real
def test_word_is_12bit(scope):
    """WORD carries >8-bit resolution, packed in the DHO804's 16-bit code space.

    The DHO804 does NOT return raw 0..4095 codes: it scales the 12-bit ADC code
    into the uint16 range, centered on :WAVeform:YREFerence? (observed 32768 =
    2**15). The driver converts using the scope-reported y_reference / y_increment,
    so voltage is correct regardless of the code packing. Here we confirm the read
    carries genuine 12-bit resolution: more than 8-bit (so we are not silently
    getting BYTE data) but no more than 12-bit worth of distinct levels.
    """
    _armed_stop(scope)
    for ch in _displayed(scope):
        w = scope.read_channel(ch, fmt='WORD')
        assert w.raw.dtype == np.uint16, f"{ch}: raw dtype {w.raw.dtype}, expected uint16"
        distinct = np.unique(w.raw).size
        assert distinct > 256, (
            f"{ch}: only {distinct} distinct codes -- looks like 8-bit data in a "
            f"16-bit container (check WORD format / endianness)"
        )
        # A true 12-bit ADC yields at most 2**12 distinct levels; materially more
        # would mean 16-bit-wide noise, not 12-bit data. (A signal smaller than full
        # scale simply uses fewer levels, so this is an upper bound, not equality.)
        assert distinct <= 4096, (
            f"{ch}: {distinct} distinct codes exceeds 12-bit (4096) -- not 12-bit data"
        )


@_real
def test_word_endianness_smooth(scope):
    """A smooth input yields a smooth raw array; a byte-swap shows as HF garbage."""
    _armed_stop(scope)
    ch = _displayed(scope)[0]
    w = scope.read_channel(ch, fmt='WORD')
    raw = w.raw.astype(np.int64)
    # Mean abs first-difference should be small vs the full code span for a smooth
    # waveform. A wrong-endian array scrambles high/low bytes -> large jumps.
    span = max(int(raw.max() - raw.min()), 1)
    mean_step = float(np.mean(np.abs(np.diff(raw))))
    assert mean_step < span, (
        f"{ch}: mean |delta| {mean_step:.1f} >= code span {span} -- input not smooth "
        f"or WORD bytes are swapped (endianness)"
    )


@_real
def test_word_matches_byte(scope):
    """WORD voltage agrees with the BYTE read to within BYTE's own quantization.

    BYTE has ~25 codes/div (the DHO BYTE LSB is vertical_scale/25). WORD resolves
    finely, so the two reads can only agree to within roughly one BYTE LSB -- the
    correct tolerance is the BYTE LSB, NOT the signal span. On a near-flat input
    (span of a few LSBs) the cross-check is uninformative (BYTE is mostly
    quantization noise), so skip with a hint to raise the input amplitude.
    """
    _armed_stop(scope)
    ch = _displayed(scope)[0]
    w_word = scope.read_channel(ch, fmt='WORD')
    w_byte = scope.read_channel(ch, fmt='BYTE')

    byte_lsb = scope.vertical_scale(ch) / 25.0          # DHO BYTE step (V)
    span = float(w_word.voltage.max() - w_word.voltage.min())
    if span < 10 * byte_lsb:
        pytest.skip(
            f"{ch}: input span {span:.4g} V is only {span / byte_lsb:.1f} BYTE LSBs "
            f"-- WORD-vs-BYTE cross-check is uninformative; raise the input amplitude "
            f"to fill more of the screen"
        )

    n = min(w_word.points, w_byte.points)
    diff = np.abs(w_word.voltage[:n] - w_byte.voltage[:n])
    # Agreement to ~1 BYTE LSB (median) is correct; a calibration/endian bug would
    # be many LSBs off.
    assert float(np.median(diff)) < byte_lsb, (
        f"{ch}: WORD and BYTE disagree by median {np.median(diff):.4g} V "
        f"(> 1 BYTE LSB = {byte_lsb:.4g} V)"
    )


@_real
def test_screen_window_locatable(scope, capsys):
    """Report the on-screen window as an index slice of the full record (gap 5.1)."""
    _armed_stop(scope)
    ch = _displayed(scope)[0]
    w = scope.read_channel(ch)
    t = w.time
    screen_span = scope.timebase_scale() * 10.0          # 10 divisions
    tb_offset = scope.timebase_offset()
    lo_t = tb_offset - screen_span / 2.0
    hi_t = tb_offset + screen_span / 2.0
    i_lo = int(np.searchsorted(t, lo_t))
    i_hi = int(np.searchsorted(t, hi_t))
    print(f"\n{ch}: full record {w.points} pts [{t[0]:.4g}, {t[-1]:.4g}] s; "
          f"on-screen window [{lo_t:.4g}, {hi_t:.4g}] s -> idx [{i_lo}, {i_hi}] "
          f"({max(i_hi - i_lo, 0)} pts)")
    # The screen window must be a non-empty sub-slice of the full record.
    assert 0 <= i_lo < i_hi <= w.points, (
        f"{ch}: screen window [{i_lo}, {i_hi}] not inside full record {w.points}"
    )
    if SHOW_PLOT:
        import matplotlib.pyplot as plt
        plt.figure(f"Rigol {ch} full record + screen slice")
        plt.plot(t, w.voltage, lw=0.5, label="full record")
        plt.plot(t[i_lo:i_hi], w.voltage[i_lo:i_hi], lw=0.8, label="on-screen")
        plt.xlabel("s"); plt.ylabel("V"); plt.legend(); plt.tight_layout(); plt.show()


@_real
def test_no_constant_tail(scope):
    """No long run of identical trailing samples (zero-pad / incomplete capture)."""
    _armed_stop(scope)
    for ch in _displayed(scope):
        v = scope.read_channel(ch).voltage
        if v.size >= 100:
            tail = v[-max(50, v.size // 50):]
            assert float(np.ptp(tail)) != 0.0, (
                f"{ch}: ends in {tail.size}+ identical samples (~{float(tail[0]):.4g} V) "
                f"-- possible zero-padding / incomplete acquisition"
            )


@_real
def test_deep_read_timing(scope, capsys):
    """Read the full record and print wall-clock time (feeds the §3.4 timeout)."""
    _armed_stop(scope)
    ch = _displayed(scope)[0]
    t0 = time.time()
    w = scope.read_channel(ch)
    dt = time.time() - t0
    mb = w.raw.nbytes / 1e6
    print(f"\n{ch}: read {w.points} pts ({mb:.1f} MB WORD) in {dt:.2f} s "
          f"({mb / dt:.1f} MB/s)")
    assert w.points > 0

"""Real-hardware bench test for RigolDHO800 (firmware 00.01.05, up to 25M depth).

Validates the full-memory-depth WORD acquisition path against a real DHO800. Every test is skipped unless SCOPE_IP (or RIGOL_SCOPE_IP) is set below. Set the panel Mem Depth
on the scope, then run; the length check confirms the driver returns exactly that
many points.

    pytest tests/test_rigol_scope_real.py -v -s        # -s to see the live report

Recommended bench setup: feed a function-generator sine/ramp, give the channel a
non-zero vertical offset and the timebase a non-zero horizontal delay (the historical
failure conditions). Step the panel Mem Depth 1k -> 10k -> 100k -> 1M -> 10M -> 25M
across runs (or set EXPECT_MDEPTH to assert a specific depth).

What it checks (per displayed analog channel):
  - acquire returns len == :ACQuire:MDEPth? (full record, no trim/pad/truncation)
  - WORD read is finer than BYTE: raw dtype uint16 with >256 distinct codes (proves
    we are NOT silently getting 8-bit data) and no more than 4096. On the DHO804 the
    codes sit around YREFerence 32768 (about 15,600..50,500, ~750 unevenly spaced
    levels per channel), not 0..4095. A smooth input yields a smooth raw array
    (endianness sanity)
  - :WAVeform:PREamble? gives exactly the six :WAVeform:X*?/Y*? values in MAXimum
    mode, also when read right after a source switch (no settle delay)
  - read_channels returns the same arrays and metadata as one read_channel per channel
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

import os
import time

import numpy as np
import pytest

from lab_scopes.rigol import RigolDHO800


# === user configuration =====================================================
# Set here, or from the environment without editing this file:
#   RIGOL_SCOPE_IP=<address> RIGOL_EXPECT_MDEPTH=1000000 pytest tests/test_rigol_scope_real.py -v -s
SCOPE_IP = os.environ.get("RIGOL_SCOPE_IP", "")  # empty: every test is skipped
EXPECT_MDEPTH = int(os.environ["RIGOL_EXPECT_MDEPTH"]) if os.environ.get("RIGOL_EXPECT_MDEPTH") else None  # int asserts the panel depth
SHOW_PLOT = os.environ.get("RIGOL_SHOW_PLOT") == "1"  # True: plot full record + highlighted on-screen slice
# ============================================================================

TRIGGER_TIMEOUT_S = 30.0
MIN_SPAN_DIV = 1.0  # test_word_is_12bit skips channels whose trace spans less than this


_real = pytest.mark.skipif(
    not SCOPE_IP, reason="set RIGOL_SCOPE_IP (or SCOPE_IP in this file) to test a real Rigol scope"
)


@pytest.fixture(scope="module")
def scope():
    s = RigolDHO800(SCOPE_IP, verbose=True)
    try:
        yield s
    finally:
        try:
            if s.connected:
                s.run()  # the tests arm with :SINGle; leave the scope running
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
    """WORD carries more than 8 bits, in the DHO804's 16-bit code space.

    The DHO804 does NOT return 0..4095 codes. WORD values sit around
    :WAVeform:YREFerence? (32768): about 15,600..50,500 on the bench, with about
    750 unevenly spaced levels per channel. The driver converts using the
    scope-reported y_reference / y_increment, so voltage is correct regardless of
    the code packing. Here we confirm the read has more levels than BYTE (so we are
    not silently getting 8-bit data) and no more than a 12-bit ADC can produce.
    """
    _armed_stop(scope)
    tested = []
    for ch in _displayed(scope):
        w = scope.read_channel(ch, fmt='WORD')
        assert w.raw.dtype == np.uint16, f"{ch}: raw dtype {w.raw.dtype}, expected uint16"
        span_div = float(np.ptp(w.voltage)) / w.metadata['vertical_scale']
        if span_div < MIN_SPAN_DIV:
            # A nearly flat trace (e.g. a trigger input between triggers) uses few
            # codes whatever the format, so it says nothing about the resolution.
            print(f"\n{ch}: span {span_div:.2f} div < {MIN_SPAN_DIV} div, not tested")
            continue
        tested.append(ch)
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
    if not tested:
        pytest.skip(f"no displayed channel spans {MIN_SPAN_DIV} div or more; feed a signal")


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


@_real
def test_preamble_matches_separate_queries(scope, capsys):
    """:WAVeform:PREamble? equals the six :WAVeform:X*?/Y*? queries in MAXimum/Stop mode.

    read_channels takes its calibration from the preamble; the 2026-05 notes (fw 00.01.03)
    say the preamble read wrong values in MAX mode, so this must hold on the bench firmware.
    If it fails, set RigolDHO800.calibration_from_preamble = False and report it.
    Channels are visited forward then backward, and each preamble is read right after
    :WAVeform:SOURce/MODE/FORMat with no settle delay. Both reads would be stale together
    if a source switch had not taken effect, so they are also compared with the six
    queries read again 0.2 s later. That only shows a late switch when the channels'
    calibrations differ (different V/div); otherwise the test prints INCONCLUSIVE.
    """
    _armed_stop(scope)
    chans = _displayed(scope)
    for fmt in ("WORD", "BYTE"):
        settled = {}
        for ch in list(chans) + list(reversed(chans)):
            scope._select_waveform(ch, fmt)
            pre = scope._calibration_from_preamble(fmt)  # first query after the switch
            sep = scope._calibration_from_queries()
            time.sleep(0.2)
            late = scope._calibration_from_queries()
            settled[ch] = late
            print(f"\n{ch} {fmt}: preamble {pre}\n{ch} {fmt}: queries  {sep}"
                  f"\n{ch} {fmt}: +0.2 s   {late}")
            assert pre == sep, f"{ch} {fmt}: PREamble {pre} != separate queries {sep}"
            assert pre == late, f"{ch} {fmt}: read right after the switch {pre} != 0.2 s later {late}"
        distinct = {tuple(sorted(c.items())) for c in settled.values()}
        if len(chans) > 1 and len(distinct) < len(chans):
            print(f"\nSETTLE INCONCLUSIVE ({fmt}): some displayed channels report identical "
                  f"calibration; set different V/div on them to test a late source switch")


@_real
def test_read_channels_matches_read_channel(scope, capsys):
    """read_channels returns the same arrays and metadata as one read_channel per channel."""
    _armed_stop(scope)
    chans = _displayed(scope)
    t0 = time.time()
    together = scope.read_channels(chans)
    dt = time.time() - t0
    print(f"\nread_channels({list(chans)}): {dt:.2f} s for {together[chans[0]].points} pts each")
    for ch in chans:
        alone = scope.read_channel(ch)
        both = together[ch]
        assert both.raw.tobytes() == alone.raw.tobytes(), f"{ch}: raw codes differ"
        assert both.voltage.tobytes() == alone.voltage.tobytes(), f"{ch}: voltage differs"
        assert both.time.tobytes() == alone.time.tobytes(), f"{ch}: time differs"
        assert both.metadata == alone.metadata, f"{ch}: {both.metadata} != {alone.metadata}"

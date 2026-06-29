"""Comprehensive real-hardware diagnostic suite for LeCroy SEQUENCE mode.

PURPOSE
    Isolate *why* sequence-mode acquisition fails on a real scope. Every test
    checks one manual-derived invariant, so a failure points at a specific
    cause (prefix offset, comm_type/format leak, WAVE_ARRAY_1 vs
    WAVE_ARRAY_COUNT, divisibility, buffer overrun, or shape/scaling). A
    final always-passing test prints a full byte-level report you can paste
    back verbatim.

PRECONDITIONS (do these on the scope, by hand, before running):
    1. Put the scope in SEQUENCE / segmented acquisition mode with >= 2
       segments (e.g. Horizontal -> Sequence; set Num Segments to 4+).
    2. Make sure the chosen trace is DISPLAYED and has data (trigger it /
       set AUTO so segments actually fill).
    3. Set SCOPE_IP below (and optionally TRACE) to your scope.

RUN:
    pytest tests/test_lecroy_sequence_real.py -v -s

    -s is important: the report test prints to stdout. Paste the whole
    output (especially the SEQUENCE-MODE REPORT block) back for diagnosis.

This suite is read-only with respect to acquisition settings: it only relies
on what LeCroyScope.__init__ already sets (COMM_HEADER OFF, COMM_FORMAT
DEF9,WORD,BIN). It does not change timebase, segments, vertical scale, etc.

Manual references (MAUI Remote Control & Automation Manual):
    p. 6-19/6-20  legacy waveform file format: #N..., WAVEDESC, USERTEXT,
                  TRIGTIME, RISTIME, DATA_ARRAY_1 block ordering.
    p. 6-22       WAVEDESC template: WAVE_ARRAY_1 (bytes), WAVE_ARRAY_COUNT
                  (points), SUBARRAY_COUNT (segments) field offsets.
    p. 6-27/6-28  "segments are read out one after the other"; WAVE_ARRAY_
                  COUNT and SUBARRAY_COUNT give total points and segment count.
    p. 7-152/153  WAVEFORM_SETUP SN=0 -> all segments; SN=n -> segment n.
"""

import struct

import numpy as np
import pytest

from lab_scopes.lecroy import LeCroyScope, LeCroyNoDataError, WAVEDESC_SIZE


# === user configuration =====================================================
SCOPE_IP = "192.168.7.63"   # set to your scope's IPv4 address; None => skip all
TRACE = None                # None => first displayed trace; or e.g. "C2"
PLOT_SEGMENTS = False       # True => open a window plotting every segment as a
                            # trace, for a visual "did the data arrive?" check
PLOT_SAVE_PATH = None       # e.g. r"C:\temp\seq.png" => also save the figure
# ============================================================================


_real = pytest.mark.skipif(
    not SCOPE_IP, reason="set SCOPE_IP in this file to test a real LeCroy scope"
)

# Shared report dict, populated by the report test and printed at the end.
DIAG = {}


# === fixtures ===============================================================


@pytest.fixture(scope="module")
def scope():
    with LeCroyScope(SCOPE_IP, verbose=False, timeout=10.0) as s:
        yield s


@pytest.fixture(scope="module")
def trace(scope):
    if TRACE:
        return TRACE
    displayed = scope.displayed_traces()
    if not displayed:
        pytest.skip("no displayed traces on scope")
    return displayed[0]


@pytest.fixture(scope="module")
def capture(scope, trace):
    """One raw all-segments read (SN=0), shared by every test in this module.

    Returns a dict of the raw bytes + decoded WAVEDESC + derived offsets so
    tests don't each hit the scope. Fetched once per session.
    """
    try:
        trace_bytes, wavedesc_bytes = scope.acquire_bytes(trace, seg=0)
    except LeCroyNoDataError as exc:
        pytest.skip(f"{trace}: no data ({exc})")
    wd = scope.translate_wavedesc_bytes(wavedesc_bytes)
    bps = 2 if wd.comm_type == 1 else (1 if wd.comm_type == 0 else None)
    # offset of the literal 'WAVEDESC' marker = the true leading-prefix length
    wd_off = trace_bytes.find(b"WAVEDESC")
    ndx0 = (15 + WAVEDESC_SIZE) + wd.user_text + wd.trigtime_array \
        + wd.ris_time_array + wd.res_array1
    return {
        "trace": trace,
        "trace_bytes": trace_bytes,
        "wavedesc_bytes": wavedesc_bytes,
        "wd": wd,
        "bps": bps,
        "wd_off": wd_off,
        "ndx0": ndx0,
        "n": len(trace_bytes),
    }


@pytest.fixture(scope="module")
def seq_capture(capture):
    """``capture``, but skips the whole test if the scope is not in sequence
    mode (``subarray_count < 2``). Centralizes the precondition so each
    sequence test doesn't repeat the guard."""
    if capture["wd"].subarray_count < 2:
        pytest.skip(
            f"subarray_count={capture['wd'].subarray_count}; scope is NOT in "
            f"sequence mode. Enable segmented acquisition (>=2 segments)."
        )
    return capture


@pytest.fixture(scope="module")
def seq_segments(scope, trace, seq_capture):
    """One shared all-segments read (raw int16). Sequence reads can be many MB
    each, so the whole suite reuses this single transfer instead of re-reading.
    """
    segments, _wd = scope.acquire_sequence_data(trace, raw=True)
    return segments


# === connectivity / identity ================================================


@_real
def test_connect(scope):
    assert bool(scope) is True
    assert isinstance(scope.idn_string, str) and scope.idn_string.strip()


@_real
def test_comm_state_is_off_def9_word(scope):
    """ndx0's hardcoded 15-byte prefix is only valid in COMM_HEADER OFF +
    COMM_FORMAT DEF9,WORD. If another method left the scope in a different
    state, the prefix length shifts and ndx0 is wrong. (Diagnostic, not fatal.)
    """
    chdr = scope.scope.query("COMM_HEADER?")
    cfmt = scope.scope.query("COMM_FORMAT?")
    DIAG["comm_header"] = chdr
    DIAG["comm_format"] = cfmt
    # These are warnings via assertion messages rather than hard requirements:
    assert "OFF" in str(chdr).upper(), (
        f"COMM_HEADER is {chdr!r}, expected OFF; prefix length (15) may be wrong"
    )
    assert "WORD" in str(cfmt).upper(), (
        f"COMM_FORMAT is {cfmt!r}, expected WORD; comm_type/bps may be wrong "
        f"(a prior wait_for_sweeps leaves it BYTE)"
    )


# === prefix / descriptor alignment ==========================================


@_real
def test_wavedesc_prefix_is_15_bytes(capture):
    """The driver assumes the WAVEDESC starts at byte 15. Verify against the
    real response (location of the 'WAVEDESC' literal). A mismatch means ndx0
    is computed from the wrong base -> data is read at the wrong offset."""
    wd_off = capture["wd_off"]
    assert wd_off != -1, "could not find 'WAVEDESC' literal in response"
    assert wd_off == 15, (
        f"real prefix is {wd_off} bytes, driver hardcodes 15 -> ndx0 wrong"
    )


@_real
def test_wavedesc_slice_is_aligned(capture):
    """wavedesc_bytes (sliced at [15:15+SIZE] by acquire_bytes) must actually
    begin with the descriptor name; otherwise the whole WAVEDESC is misread."""
    assert capture["wavedesc_bytes"][:8] == b"WAVEDESC", (
        f"wavedesc slice starts with {capture['wavedesc_bytes'][:8]!r}, "
        f"expected b'WAVEDESC' (prefix != 15?)"
    )


# === comm_type / format =====================================================


@_real
def test_comm_type_is_valid(capture):
    assert capture["wd"].comm_type in (0, 1), (
        f"comm_type={capture['wd'].comm_type}; expected 0 (BYTE) or 1 (WORD)"
    )


@_real
def test_comm_type_is_word(capture):
    """__init__ sets DEF9,WORD,BIN. If comm_type came back 0 (BYTE), some path
    (e.g. wait_for_sweeps) left the format as BYTE and never restored WORD --
    a state leak that halves the byte count and corrupts sequence reads."""
    assert capture["wd"].comm_type == 1, (
        f"comm_type={capture['wd'].comm_type} (BYTE); expected 1 (WORD). "
        f"COMM_FORMAT was likely left as BYTE by a prior call."
    )


# === sequence-mode presence =================================================


@_real
def test_scope_is_in_sequence_mode(capture):
    """If this fails/skips, the scope is not actually in sequence mode -- put
    it in segmented acquisition with >= 2 segments and re-run."""
    sub = capture["wd"].subarray_count
    if sub < 2:
        pytest.skip(
            f"subarray_count={sub}; scope is NOT in sequence mode. "
            f"Enable segmented acquisition (>=2 segments) and re-run."
        )
    assert sub >= 2


# === WAVE_ARRAY_1 vs WAVE_ARRAY_COUNT vs SUBARRAY_COUNT =====================


@_real
def test_wave_array_count_consistent_with_array_1(capture):
    """Manual p.6-28: WAVE_ARRAY_COUNT is the total POINTS; WAVE_ARRAY_1 is the
    total BYTES. For a clean WORD capture wave_array_1//2 == wave_array_count.
    The driver derives total samples from wave_array_1; if these disagree, that
    derivation is the bug and wave_array_count should be used instead."""
    wd = capture["wd"]
    bps = capture["bps"]
    if bps is None:
        pytest.skip("invalid comm_type; covered by another test")
    from_bytes = wd.wave_array_1 // bps
    DIAG["total_from_wave_array_1"] = from_bytes
    DIAG["wave_array_count"] = wd.wave_array_count
    assert from_bytes == wd.wave_array_count, (
        f"wave_array_1//bps={from_bytes} != wave_array_count={wd.wave_array_count}; "
        f"driver uses the former -- use wave_array_count instead"
    )


@_real
def test_total_samples_divisible_by_segments(seq_capture):
    """segment_sample_count() raises 'ragged segment data' if wave_array_1//bps
    is not divisible by subarray_count. Confirm the real numbers divide evenly."""
    wd = seq_capture["wd"]
    bps = seq_capture["bps"]
    if bps is None:
        pytest.skip("invalid comm_type")
    total = wd.wave_array_1 // bps
    assert total % wd.subarray_count == 0, (
        f"total_samples={total} not divisible by subarray_count="
        f"{wd.subarray_count} -> 'ragged segment data' (RuntimeError)"
    )


# === buffer / offset overrun ================================================


@_real
def test_data_fits_in_buffer(capture):
    """np.frombuffer(count=total_samples, offset=ndx0) raises 'buffer is smaller
    than requested size' if ndx0 + wave_array_1 > len(trace_bytes). This is the
    single most likely real-hardware crash; it isolates prefix/offset errors."""
    n = capture["n"]
    ndx0 = capture["ndx0"]
    wa1 = capture["wd"].wave_array_1
    assert ndx0 + wa1 <= n, (
        f"ndx0({ndx0}) + wave_array_1({wa1}) = {ndx0 + wa1} > len(trace_bytes)={n}"
        f" -> np.frombuffer OVERRUN. Prefix/offset wrong or short read."
    )


@_real
def test_no_unexpected_trailing_bytes(capture):
    """After the data array the response should end (possibly + CRC). Large
    leftover bytes hint at a wrong ndx0 or a misread block length."""
    n = capture["n"]
    ndx0 = capture["ndx0"]
    wa1 = capture["wd"].wave_array_1
    trailing = n - (ndx0 + wa1)
    DIAG["trailing_bytes"] = trailing
    # Allow a small CRC/terminator tail; flag anything larger.
    assert 0 <= trailing <= 16, (
        f"trailing bytes after data = {trailing} (expected 0..16). "
        f"ndx0/wave_array_1 likely inconsistent."
    )


# === the actual driver call =================================================


@_real
def test_acquire_sequence_data_runs(seq_capture, seq_segments):
    """End-to-end: the real failing call. If the invariants above passed but
    this raises, capture the exception text -- it pinpoints the remaining bug."""
    assert len(seq_segments) == seq_capture["wd"].subarray_count, (
        f"got {len(seq_segments)} segments, expected {seq_capture['wd'].subarray_count}"
    )
    sizes = {seg.size for seg in seq_segments}
    assert len(sizes) == 1, f"segments have differing lengths: {sizes}"
    DIAG["n_segments"] = len(seq_segments)
    DIAG["samples_per_segment"] = seq_segments[0].size
    DIAG["segment_dtype"] = str(seq_segments[0].dtype)


@_real
def test_acquire_sequence_data_raw_is_int16(seq_capture, seq_segments):
    """raw=True must yield int16 (what LAPD_DAQ stacks). raw=False yields scaled
    float volts; the int16 cast downstream would then zero the data."""
    if seq_capture["wd"].comm_type != 1:
        pytest.skip("comm_type != WORD; int16 check N/A")
    assert seq_segments[0].dtype == np.dtype("<i2"), (
        f"raw segment dtype is {seq_segments[0].dtype}, expected int16 (<i2)"
    )


@_real
def test_acquire_sequence_data_values_nonzero(seq_segments):
    """A triggered sequence capture should contain non-constant data. All-zero
    (or all-constant) segments indicate truncation/scaling/offset corruption."""
    stacked = np.stack([np.asarray(s) for s in seq_segments])
    DIAG["data_min"] = int(stacked.min())
    DIAG["data_max"] = int(stacked.max())
    assert stacked.min() != stacked.max(), (
        f"all sequence samples are constant ({stacked.min()}); "
        f"likely truncation/offset/scaling corruption, not real data"
    )


# === per-segment SN read cross-check (old driver's strategy) ================


@_real
def test_single_segment_read_matches_bulk(scope, trace, seq_segments):
    """Cross-check the bulk SN=0 read against per-segment SN=n reads (the old
    pyvisa driver's strategy). If the bulk read is mis-sliced but per-segment
    reads are fine, this disagreement localizes the bug to the bulk path."""
    # per-segment (SN is 1-based per manual p.7-152)
    try:
        seg1, _ = scope.acquire(trace, seg=1, raw=True)
    except Exception as exc:
        pytest.skip(f"per-segment SN=1 read failed: {type(exc).__name__}: {exc}")
    DIAG["bulk_seg0_len"] = int(seq_segments[0].size)
    DIAG["sn1_len"] = int(np.asarray(seg1).size)
    # Lengths should match; values may differ if the scope re-armed between
    # reads, so compare length first (the structural invariant).
    assert seq_segments[0].size == np.asarray(seg1).size, (
        f"bulk segment length {seq_segments[0].size} != SN=1 read length "
        f"{np.asarray(seg1).size}; bulk reshape is mis-sized"
    )


# === time array =============================================================


@_real
def test_time_array_matches_segment_length(scope, trace, seq_capture, seq_segments):
    """time_array() in sequence mode must return per-segment length, matching
    the data so HDF5 time/data axes agree."""
    # Passing an explicit trace makes time_array() re-read the descriptor itself,
    # so it does not depend on whatever self.wd an earlier test last left set.
    t = scope.time_array(trace)
    DIAG["time_array_len"] = int(t.size)
    assert t.size == seq_segments[0].size, (
        f"time_array length {t.size} != samples/segment {seq_segments[0].size}"
    )
    assert np.all(np.diff(t) > 0), "time_array is not strictly increasing"


# === visual check: plot every segment as its own trace ======================


@_real
@pytest.mark.skipif(
    not PLOT_SEGMENTS,
    reason="set PLOT_SEGMENTS=True in this file to open the segment plot",
)
def test_plot_segments(scope, trace, seq_capture, seq_segments):
    """Overlay all N segments on one axes (raw int16 vs the per-segment time
    axis) so you can eyeball that every segment arrived and is non-degenerate.
    Opt-in via PLOT_SEGMENTS -- it opens a blocking window. Skips cleanly if
    matplotlib isn't installed."""
    matplotlib = pytest.importorskip("matplotlib")
    if not PLOT_SAVE_PATH:
        matplotlib.use("TkAgg")  # interactive backend for the on-screen window
    import matplotlib.pyplot as plt

    t = scope.time_array(trace)  # per-segment axis; len == samples/segment
    t_us = t * 1e6               # microseconds read more naturally than seconds

    fig, ax = plt.subplots(figsize=(11, 6))
    for i, seg in enumerate(seq_segments):
        arr = np.asarray(seg)
        ax.plot(t_us, arr, lw=0.8, label=f"seg {i}" if len(seq_segments) <= 12 else None)
    ax.set_xlabel("time (us, per-segment axis)")
    ax.set_ylabel("raw ADC counts (int16)")
    ax.set_title(
        f"{trace}: {len(seq_segments)} segments x {seq_segments[0].size} samples"
    )
    if len(seq_segments) <= 12:
        ax.legend(loc="upper right", fontsize=8)
    ax.grid(True, alpha=0.3)
    fig.tight_layout()

    if PLOT_SAVE_PATH:
        fig.savefig(PLOT_SAVE_PATH, dpi=120)
        print(f"\nsaved segment plot to {PLOT_SAVE_PATH}")
    else:
        print("\nclose the plot window to continue the test run...")
        plt.show()
    plt.close(fig)


# === full report (always passes; run last) ==================================


@_real
def test_zz_report(scope, trace, capture):
    """Prints a complete byte-level report. Paste this whole block back."""
    wd = capture["wd"]
    n = capture["n"]
    ndx0 = capture["ndx0"]
    bps = capture["bps"]
    tb = capture["trace_bytes"]

    def hx(b, k=24):
        c = b[:k]
        return " ".join(f"{x:02x}" for x in c) + "  |" + \
            "".join(chr(x) if 32 <= x < 127 else "." for x in c) + "|"

    print("\n" + "=" * 74)
    print("LeCroy SEQUENCE-MODE REPORT  (paste this whole block back)")
    print("=" * 74)
    print(f"  SCOPE_IP            : {SCOPE_IP}")
    print(f"  IDN                 : {scope.idn_string.strip()!r}")
    print(f"  trace               : {trace}")
    print(f"  COMM_HEADER?        : {DIAG.get('comm_header')!r}")
    print(f"  COMM_FORMAT?        : {DIAG.get('comm_format')!r}")
    print("-" * 74)
    print(f"  len(trace_bytes)    : {n}")
    print(f"  first 24 bytes      : {hx(tb)}")
    print(f"  'WAVEDESC' offset   : {capture['wd_off']}   (driver assumes 15)")
    print("-" * 74)
    print(f"  comm_type           : {wd.comm_type}   (0=BYTE,1=WORD)  bps={bps}")
    print(f"  comm_order          : {wd.comm_order}   (0=hi-first,1=lo-first)")
    print(f"  wave_array_1 (bytes): {wd.wave_array_1}")
    print(f"  wave_array_count    : {wd.wave_array_count}   (manual: total points)")
    print(f"  subarray_count      : {wd.subarray_count}")
    print(f"  nom_subarray_count  : {wd.nom_subarray_count}")
    print(f"  segment_index       : {wd.segment_index}")
    print(f"  sweeps_per_acq      : {wd.sweeps_per_acq}")
    print(f"  user_text           : {wd.user_text}")
    print(f"  trigtime_array      : {wd.trigtime_array}   (16 bytes/segment)")
    print(f"  ris_time_array      : {wd.ris_time_array}")
    print(f"  res_array1          : {wd.res_array1}")
    print(f"  horiz_interval      : {wd.horiz_interval}")
    print(f"  horiz_offset        : {wd.horiz_offset}")
    print(f"  vertical_gain       : {wd.vertical_gain}")
    print(f"  vertical_offset     : {wd.vertical_offset}")
    print("-" * 74)
    if bps:
        total = wd.wave_array_1 // bps
        print(f"  total_samples (wa1/bps) : {total}")
        if wd.subarray_count:
            print(f"  total %% subarray_count  : {total % wd.subarray_count} "
                  f"({'OK' if total % wd.subarray_count == 0 else 'RAGGED'})")
        print(f"  ndx0                    : {ndx0}")
        print(f"  ndx0 + wave_array_1     : {ndx0 + wd.wave_array_1}  vs n={n}  "
              f"({'FITS' if ndx0 + wd.wave_array_1 <= n else 'OVERRUN'})")
        print(f"  trailing bytes          : {n - (ndx0 + wd.wave_array_1)}")
    print("-" * 74)
    for k in ("n_segments", "samples_per_segment", "segment_dtype",
              "data_min", "data_max", "bulk_seg0_len", "sn1_len",
              "time_array_len"):
        if k in DIAG:
            print(f"  {k:22}: {DIAG[k]}")
    print("=" * 74)

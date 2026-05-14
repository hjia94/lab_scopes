"""Comprehensive real-hardware test suite for LeCroyScope.

Edit the two constants below to enable / configure.

    SCOPE_IP     - set to the scope's IPv4 address ("192.168.1.100") to enable
                   the suite. Leave as None to keep every test skipped.
    DESTRUCTIVE  - False: only read-only and read+restore tests run.
                   True : also runs setters that mutate scope state and the
                          ~15 s self-calibration. Only use on a dedicated
                          test scope.

Run with `-s` to see the end-of-session report live:
    pytest tests/test_lecroy_scope_real.py -v -s
"""

import time

import numpy as np
import pytest

from lab_scopes.lecroy import LeCroyHeader, LeCroyScope


# === user configuration =====================================================
SCOPE_IP = "192.168.7.63"
DESTRUCTIVE = False
# ============================================================================


_real = pytest.mark.skipif(
    not SCOPE_IP, reason="set SCOPE_IP in this test file to test a real LeCroy scope"
)
_destructive = pytest.mark.skipif(
    not DESTRUCTIVE,
    reason="DESTRUCTIVE=False; skipping state-mutating test",
)


REPORT = {"tests": {}, "traces": {}, "timings": {}, "warnings": []}


def _note(name, msg):
    REPORT["tests"][name] = ("PASS", msg)


def _warn(msg):
    REPORT["warnings"].append(msg)


# === fixtures ===============================================================


@pytest.fixture(scope="module")
def scope():
    with LeCroyScope(SCOPE_IP, verbose=False, timeout=10.0) as s:
        yield s


@pytest.fixture(scope="module")
def any_displayed_trace(scope):
    traces = scope.displayed_traces()
    if not traces:
        pytest.skip("no displayed traces on scope")
    return traces[0]


@pytest.fixture(scope="module")
def any_displayed_channel(scope):
    chans = scope.displayed_channels()
    if not chans:
        pytest.skip("no displayed channels on scope")
    return chans[0]


@pytest.fixture(scope="module", autouse=True)
def _final_report():
    yield
    if not SCOPE_IP:
        return
    _print_report()


def _record_skip(name, reason):
    REPORT["tests"][name] = ("SKIP", reason)


def _record_fail(name, reason):
    REPORT["tests"][name] = ("FAIL", reason)


# === connection / identity ==================================================


@_real
def test_connect_and_idn(scope):
    assert bool(scope) is True
    assert isinstance(scope.idn_string, str) and scope.idn_string.strip()
    assert scope.rm_list_resources() == ()
    _note("test_connect_and_idn", f"IDN={scope.idn_string.strip()!r}")


# === validation =============================================================


@_real
def test_validate_channel_accepts_ints_and_strings(scope):
    assert scope.validate_channel("C1") == "C1"
    assert scope.validate_channel(3) == "C3"
    with pytest.raises(RuntimeError):
        scope.validate_channel("C9")
    with pytest.raises(RuntimeError):
        scope.validate_channel(0)
    _note("test_validate_channel_accepts_ints_and_strings", "C1, 3->C3, rejects C9/0")


@_real
def test_validate_trace_rejects_unknown(scope):
    with pytest.raises(RuntimeError):
        scope.validate_trace("ZZ99")
    if scope.valid_trace_names:
        good = scope.valid_trace_names[0]
        assert scope.validate_trace(good) == good
    _note(
        "test_validate_trace_rejects_unknown",
        f"valid_trace_names={scope.valid_trace_names!r}",
    )


# === discovery ==============================================================


@_real
def test_valid_trace_names_populated(scope):
    assert isinstance(scope.valid_trace_names, tuple)
    if not scope.valid_trace_names:
        _warn("valid_trace_names is empty - scope may have no traces enabled")
    _note(
        "test_valid_trace_names_populated",
        f"{len(scope.valid_trace_names)} valid trace names",
    )


@_real
def test_displayed_channels_subset_of_valid(scope):
    chans = scope.displayed_channels()
    traces = scope.displayed_traces()
    assert isinstance(chans, tuple)
    assert isinstance(traces, tuple)
    for c in chans:
        assert c in ("C1", "C2", "C3", "C4")
    for t in traces:
        assert t in scope.valid_trace_names
    if len(traces) < 2:
        _warn(f"only {len(traces)} displayed trace(s); multi-trace coverage limited")
    _note(
        "test_displayed_channels_subset_of_valid",
        f"channels={chans}, traces={traces}",
    )


# === horizontal =============================================================


@_real
def test_max_samples_query_is_positive_int(scope):
    n = scope.max_samples()
    assert isinstance(n, int) and n > 0
    _note("test_max_samples_query_is_positive_int", f"max_samples={n}")


# === vertical ===============================================================


@_real
def test_vertical_scale_read(scope, any_displayed_trace):
    v = scope.vertical_scale(any_displayed_trace)
    assert isinstance(v, float) and v > 0
    _note("test_vertical_scale_read", f"{any_displayed_trace}: {v} V/div")


@_real
@_destructive
def test_set_vertical_scale_roundtrip(scope, any_displayed_trace):
    original = scope.vertical_scale(any_displayed_trace)
    try:
        target = original * 2
        scope.set_vertical_scale(any_displayed_trace, target)
        readback = scope.vertical_scale(any_displayed_trace)
        assert readback == pytest.approx(target, rel=0.2)
        _note(
            "test_set_vertical_scale_roundtrip",
            f"{any_displayed_trace}: {original}->{target} (read {readback})",
        )
    finally:
        scope.set_vertical_scale(any_displayed_trace, original)


# === averaging ==============================================================


@_real
def test_averaging_count_read(scope, any_displayed_channel):
    n = scope.averaging_count(any_displayed_channel)
    assert isinstance(n, int) and n >= 1
    _note("test_averaging_count_read", f"{any_displayed_channel}: NSweeps={n}")


@_real
@_destructive
def test_set_averaging_count_roundtrip(scope, any_displayed_channel):
    original = scope.averaging_count(any_displayed_channel)
    try:
        scope.set_averaging_count(any_displayed_channel, 4)
        assert scope.averaging_count(any_displayed_channel) == 4
        _note(
            "test_set_averaging_count_roundtrip",
            f"{any_displayed_channel}: {original}->4 ok",
        )
    finally:
        scope.set_averaging_count(any_displayed_channel, max(original, 1))


@_real
def test_max_averaging_count_matches_displayed(scope):
    if not scope.displayed_channels():
        pytest.skip("no displayed channels")
    n, ch = scope.max_averaging_count()
    assert isinstance(n, int) and n >= 1
    assert ch in scope.displayed_channels()
    _note("test_max_averaging_count_matches_displayed", f"max={n} on {ch}")


# === acquisition ============================================================


@_real
def test_acquire_bytes_header_size(scope, any_displayed_trace):
    from lab_scopes.lecroy import WAVEDESC_SIZE

    trace_bytes, header_bytes = scope.acquire_bytes(any_displayed_trace)
    assert len(header_bytes) == WAVEDESC_SIZE
    assert len(trace_bytes) > WAVEDESC_SIZE
    _note(
        "test_acquire_bytes_header_size",
        f"{any_displayed_trace}: {len(trace_bytes)} bytes total",
    )


@_real
def test_acquire_raw_int16(scope, any_displayed_trace):
    data, header_bytes = scope.acquire(any_displayed_trace, raw=True)
    assert data.dtype == np.dtype("<i2")
    assert data.size > 0
    assert len(header_bytes) > 0
    _note(
        "test_acquire_raw_int16",
        f"{any_displayed_trace}: {data.size} samples int16",
    )


@_real
def test_acquire_scaled_is_float64(scope, any_displayed_trace):
    data, _ = scope.acquire(any_displayed_trace, raw=False)
    assert data.dtype == np.dtype(np.float64)
    assert data.size > 0
    finite = np.isfinite(data)
    assert finite.all(), "scaled waveform contains non-finite samples"
    _note(
        "test_acquire_scaled_is_float64",
        f"{any_displayed_trace}: min={data.min():.4g} V max={data.max():.4g} V",
    )


@_real
def test_scaled_matches_manual_conversion(scope, any_displayed_trace):
    """Single-fetch cross-check: re-scale raw via the documented formula and
    confirm it matches what `_parse_wave_array(raw=False)` produces on the
    same trace bytes. Avoids the double-acquire flake."""
    trace_bytes, header_bytes = scope.acquire_bytes(any_displayed_trace)
    hdr = scope.translate_header_bytes(header_bytes)
    NSamples, ndx0 = scope.parse_header(hdr)

    raw = scope._parse_wave_array(trace_bytes, hdr, NSamples, ndx0, raw=True)
    scaled = scope._parse_wave_array(trace_bytes, hdr, NSamples, ndx0, raw=False)
    expected = raw.astype(np.float64) * hdr.vertical_gain - hdr.vertical_offset

    np.testing.assert_array_equal(scaled, expected)
    _note(
        "test_scaled_matches_manual_conversion",
        f"{any_displayed_trace}: {NSamples} samples scaled exactly",
    )


# === header parsing =========================================================


@_real
def test_header_parses_with_known_fields(scope, any_displayed_trace):
    _, header_bytes = scope.acquire_bytes(any_displayed_trace)
    hdr = scope.translate_header_bytes(header_bytes)
    assert hdr.comm_type in (0, 1)
    assert hdr.wave_array_1 > 0
    assert hdr.horiz_interval > 0
    NSamples, ndx0 = scope.parse_header(hdr)
    assert NSamples > 0
    assert ndx0 >= 15 + len(header_bytes)
    _note(
        "test_header_parses_with_known_fields",
        f"comm_type={hdr.comm_type} NSamples={NSamples} dt={hdr.horiz_interval:.3g}s",
    )


# === timebase ===============================================================


@_real
def test_time_array_length_matches_samples(scope, any_displayed_trace):
    data, _ = scope.acquire(any_displayed_trace, raw=True)
    t = scope.time_array()
    assert t.size == data.size
    assert np.all(np.diff(t) > 0)
    _note(
        "test_time_array_length_matches_samples",
        f"{any_displayed_trace}: len(t)={t.size} matches data",
    )


@_real
def test_time_array_uses_horiz_interval(scope, any_displayed_trace):
    _, header_bytes = scope.acquire_bytes(any_displayed_trace)
    hdr = scope.translate_header_bytes(header_bytes)
    t = scope.time_array()
    dt_expected = float(hdr.horiz_interval)
    dt_actual = float(t[1] - t[0])
    assert dt_actual == pytest.approx(dt_expected, rel=1e-9)
    _note(
        "test_time_array_uses_horiz_interval",
        f"dt={dt_actual:.6g}s (header={dt_expected:.6g}s)",
    )


# === sequence mode ==========================================================


@_real
def test_acquire_sequence_when_subarray_count_gt_1(scope, any_displayed_trace):
    _, header_bytes = scope.acquire_bytes(any_displayed_trace)
    hdr = scope.translate_header_bytes(header_bytes)
    if hdr.subarray_count < 2:
        msg = f"subarray_count={hdr.subarray_count}; sequence mode not active"
        _record_skip("test_acquire_sequence_when_subarray_count_gt_1", msg)
        pytest.skip(msg)
    segments, _ = scope.acquire_sequence_data(any_displayed_trace)
    assert len(segments) == hdr.subarray_count
    sizes = {seg.size for seg in segments}
    assert len(sizes) == 1
    _note(
        "test_acquire_sequence_when_subarray_count_gt_1",
        f"{len(segments)} segments x {segments[0].size} samples",
    )


# === status / messages ======================================================


@_real
def test_write_status_msg_no_error(scope):
    scope.write_status_msg("lab_scopes test running")
    scope.write_status_msg("x" * 80)  # exercises the >49 truncation path
    _note("test_write_status_msg_no_error", "short + long both accepted")


@_real
def test_expanded_name_lookup(scope):
    name = scope.expanded_name("C1")
    assert isinstance(name, str) and name
    assert scope.expanded_name("nonsense_trace") == "unknown_trace_name"
    _note("test_expanded_name_lookup", f"C1 -> {name!r}")


# === destructive: trigger ===================================================


@_real
@_destructive
def test_set_trigger_mode_cycle(scope):
    original = scope.set_trigger_mode("")  # query-only path
    try:
        scope.set_trigger_mode("STOP")
        scope.set_trigger_mode("AUTO")
        assert scope.set_trigger_mode("")[0:3] in ("AUT", "STO", "NOR", "SIN")
        _note("test_set_trigger_mode_cycle", f"original={original!r}; cycled STOP/AUTO")
    finally:
        # restore to AUTO by default if we can't parse original
        restore = original.strip()[:4] if isinstance(original, str) else "AUTO"
        if restore not in ("AUTO", "NORM", "SING", "STOP"):
            restore = "AUTO"
        scope.set_trigger_mode(restore if restore != "SING" else "SINGLE")


# === destructive: calibrate =================================================


@_real
@_destructive
def test_calibrate_runs(scope):
    t0 = time.perf_counter()
    scope.calibrate(True)
    dt = time.perf_counter() - t0
    assert dt >= 10.0  # the driver sleeps 15s after issuing *CAL?
    _note("test_calibrate_runs", f"calibrate completed in {dt:.1f}s")


# === report collection (must run last) ======================================


@_real
def test_zz_collect_trace_report(scope):
    """Data-collection test (not assertion-heavy): populates the per-trace
    metadata and timing tables for the end-of-session report. Named with 'zz'
    so it sorts after the rest. Always passes; warnings are recorded for the
    report instead of failing."""
    traces = scope.displayed_traces()
    if not traces:
        _warn("test_zz_collect_trace_report: no displayed traces; report will be empty")
        _note("test_zz_collect_trace_report", "no traces to profile")
        return

    for tr in traces:
        try:
            # warm-up to avoid first-acquire startup costs
            scope.acquire_bytes(tr)

            t0 = time.perf_counter()
            trace_bytes, header_bytes = scope.acquire_bytes(tr)
            elapsed = time.perf_counter() - t0

            hdr = scope.translate_header_bytes(header_bytes)
            try:
                NSamples, _ = scope.parse_header(hdr)
            except RuntimeError as e:
                _warn(f"{tr}: parse_header failed ({e}); skipping in report")
                continue

            try:
                vscale = scope.vertical_scale(tr)
            except Exception:
                vscale = None

            wrapper = LeCroyHeader(header_bytes)

            REPORT["traces"][tr] = {
                "expanded": scope.expanded_name(tr),
                "num_samples": NSamples,
                "vertical_gain": float(hdr.vertical_gain),
                "vertical_offset": float(hdr.vertical_offset),
                "horiz_interval": float(hdr.horiz_interval),
                "sampling_rate": 1.0 / float(hdr.horiz_interval)
                if hdr.horiz_interval
                else float("nan"),
                "horiz_offset": float(hdr.horiz_offset),
                "vertical_scale_V_per_div": vscale,
                "timebase": wrapper.timebase,
                "vert_coupling": wrapper.vertical_coupling,
                "record_type": wrapper.record_type,
                "sweeps_per_acq": int(hdr.sweeps_per_acq),
                "subarray_count": int(hdr.subarray_count),
            }

            nbytes = len(trace_bytes)
            REPORT["timings"][tr] = {
                "bytes": nbytes,
                "seconds": elapsed,
                "MB_per_s": (nbytes / 1e6) / elapsed if elapsed > 0 else float("inf"),
            }
        except Exception as e:
            _warn(f"{tr}: report collection raised {type(e).__name__}: {e}")

    _note(
        "test_zz_collect_trace_report",
        f"profiled {len(REPORT['traces'])}/{len(traces)} traces",
    )


# === report printing ========================================================


def _fmt_eng(x, unit="", sig=4):
    if x is None:
        return "n/a"
    try:
        return f"{x:.{sig}g}{unit}"
    except (TypeError, ValueError):
        return str(x)


def _print_report():
    print()
    print("=" * 78)
    print("LeCroy real-scope test report")
    print("=" * 78)
    print(f"  SCOPE_IP:    {SCOPE_IP}")
    print(f"  DESTRUCTIVE: {DESTRUCTIVE}")
    print()

    # --- per-trace metadata ---
    traces = REPORT["traces"]
    if traces:
        print("Per-trace metadata")
        print("-" * 78)
        headers = [
            ("trace", 8),
            ("expanded", 14),
            ("N", 8),
            ("dt", 12),
            ("Fs", 12),
            ("Vgain", 12),
            ("Voff", 12),
            ("V/div", 10),
            ("coupling", 10),
        ]
        print("".join(h.ljust(w) for h, w in headers))
        for tr, info in traces.items():
            row = [
                (tr, 8),
                (str(info["expanded"])[:13], 14),
                (str(info["num_samples"]), 8),
                (_fmt_eng(info["horiz_interval"], "s"), 12),
                (_fmt_eng(info["sampling_rate"], "Hz"), 12),
                (_fmt_eng(info["vertical_gain"], "V"), 12),
                (_fmt_eng(info["vertical_offset"], "V"), 12),
                (_fmt_eng(info["vertical_scale_V_per_div"], "V"), 10),
                (str(info["vert_coupling"])[:9], 10),
            ]
            print("".join(str(v).ljust(w) for v, w in row))
        print()
        print("  (record_type / timebase / sweeps_per_acq / subarray_count)")
        for tr, info in traces.items():
            print(
                f"    {tr}: {info['record_type']}, {info['timebase']}, "
                f"sweeps={info['sweeps_per_acq']}, segs={info['subarray_count']}"
            )
        print()

    # --- transfer timings ---
    timings = REPORT["timings"]
    if timings:
        print("Transfer timings (single acquire_bytes, post-warmup)")
        print("-" * 78)
        print("trace   bytes        seconds      MB/s")
        for tr, t in timings.items():
            print(
                f"{tr:<8}{t['bytes']:<13}{t['seconds']:<13.4f}{t['MB_per_s']:.3f}"
            )
        print()

    # --- per-test results ---
    print("Test results")
    print("-" * 78)
    if not REPORT["tests"]:
        print("  (no tests recorded notes)")
    else:
        for name, (status, note) in REPORT["tests"].items():
            print(f"  [{status}] {name}: {note}")
    print()

    # --- warnings ---
    if REPORT["warnings"]:
        print("Warnings")
        print("-" * 78)
        for w in REPORT["warnings"]:
            print(f"  ! {w}")
        print()

    print("=" * 78)

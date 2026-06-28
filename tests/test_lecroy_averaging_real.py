"""Real-hardware diagnostic suite for LeCroy on-scope AVERAGING mode.

PURPOSE
    Companion to test_lecroy_sequence_real.py. Verifies the averaging /
    CLEAR_SWEEPS / STOP cycle that LAPD_DAQ's MODE_AVERAGE path depends on,
    so a failure points at a specific cause: max_averaging_count discovery,
    the wait actually reaching NSweeps, ending in STOP, and -- the key
    constraint -- that CLEAR_SWEEPS is ignored while in STOP so the cycle
    must un-freeze (AUTO/NORM) before clearing for the next shot.

PRECONDITIONS (do these on the scope, by hand, before running):
    1. Pick a channel and set its on-scope averaging count (Math/processing or
       Cn.AverageSweeps) to a SMALL value, e.g. 5-20, so each cycle completes
       quickly. The suite reads AverageSweeps; it does not configure it for you
       unless you set ALLOW_SET_AVERAGING = True below.
    2. Make sure that channel is DISPLAYED.
    3. Provide a trigger source so sweeps actually accumulate in NORM/AUTO
       (otherwise the wait will simply time out -- which the suite reports).
    4. Set SCOPE_IP below (and optionally CHANNEL) to your scope.

RUN:
    pytest tests/test_lecroy_averaging_real.py -v -s

    -s is important: the report test prints to stdout. Paste the whole
    output (especially the AVERAGING-MODE REPORT block) back for diagnosis.

WARNING: unlike the sequence suite, these tests are NOT read-only with respect
to acquisition state -- they change TRIG_MODE and issue CLEAR_SWEEPS, and the
fixture teardown leaves the scope free-running in AUTO. With
ALLOW_SET_AVERAGING=True the suite also overwrites the channel's AverageSweeps
and does NOT restore it.

Manual references (MAUI Remote Control & Automation Manual):
    p. 6-10/6-11  INR register; bit 0 = "new signal acquired" (read-to-clear).
    p. 6-16       sync procedure: STOP -> setup -> ARM/TRMD -> poll -> read.
    p. 7-27       TRIG_MODE AUTO/NORM/SINGLE/STOP semantics.
"""

import time

import numpy as np
import pytest

from lab_scopes.lecroy import LeCroyScope


# === user configuration =====================================================
SCOPE_IP = "192.168.7.63"   # set to your scope's IPv4 address; None => skip all
CHANNEL = None              # None => channel from max_averaging_count(); or "C1"
ALLOW_SET_AVERAGING = False # True => the suite may set a small AverageSweeps
SET_AVERAGING_TO = 10       # used only when ALLOW_SET_AVERAGING is True
WAIT_TIMEOUT = 60           # per wait_for_sweeps/wait_for_max_sweeps call (s)
CLEAR_SETTLE_S = 0.25       # settle after CLEAR_SWEEPS; matches wait_for_sweeps
# ============================================================================


_real = pytest.mark.skipif(
    not SCOPE_IP, reason="set SCOPE_IP in this file to test a real LeCroy scope"
)

# Shared report dict, populated across tests and printed at the end.
DIAG = {}


# === fixtures ===============================================================


@pytest.fixture(scope="module")
def scope():
    with LeCroyScope(SCOPE_IP, verbose=False, timeout=10.0) as s:
        if ALLOW_SET_AVERAGING:
            ch = CHANNEL or (s.displayed_channels() or ("C1",))[0]
            s.set_averaging_count(ch, SET_AVERAGING_TO)
        yield s
        # Leave the scope in a benign free-running state, not frozen. Surface a
        # warning rather than swallowing silently: a failed un-freeze leaves the
        # scope STOPped with no signal, and the error may flag a driver regression.
        try:
            s.set_trigger_mode("AUTO")
        except Exception as exc:
            print(f"warning: teardown failed to set AUTO, scope may be left STOPped: {exc}")


@pytest.fixture(scope="module")
def avg(scope):
    """The (NSweeps, channel) the driver would average on. Skips if no channel
    has AverageSweeps > 1, since the STOP-cycle path is only exercised then."""
    try:
        nsweeps, ach = scope.max_averaging_count()
    except RuntimeError as exc:
        pytest.skip(f"no displayed channels: {exc}")
    if CHANNEL:
        ach = CHANNEL
        nsweeps = scope.averaging_count(ach)
    if nsweeps <= 1:
        pytest.skip(
            f"AverageSweeps on {ach} is {nsweeps} (need > 1 to exercise the "
            f"averaging cycle). Set on-scope averaging or ALLOW_SET_AVERAGING."
        )
    DIAG["channel"] = ach
    DIAG["nsweeps"] = nsweeps
    return nsweeps, ach


@pytest.fixture(scope="module")
def completed_avg(scope, avg):
    """One completed averaging cycle, reused by every test that just needs
    *a* finished average to exist (not a fresh/back-to-back one). Mirrors how
    the sequence suite shares a single slow transfer via ``seq_segments``.

    Captures comm_type BEFORE the wait (so the WORD-state-leak test has its
    pre-cycle baseline), runs one ``wait_for_max_sweeps``, and skips the whole
    group if it times out. Leaves the scope frozen in STOP at >= NSweeps.
    """
    nsweeps, ach = avg
    comm_before = scope.translate_wavedesc_bytes(
        scope.acquire_bytes(ach, seg=0)[1]
    ).comm_type
    DIAG["comm_type_before_avg"] = comm_before
    timed_out, n = scope.wait_for_max_sweeps(timeout=WAIT_TIMEOUT)
    DIAG["wait1_timed_out"] = timed_out
    DIAG["wait1_n"] = n
    if timed_out:
        pytest.skip(
            f"averaging timed out at {n}/{nsweeps} after {WAIT_TIMEOUT}s -- "
            f"is a trigger reaching the scope?"
        )
    return {"nsweeps": nsweeps, "channel": ach, "n": n, "comm_before": comm_before}


# === item 8: averaging-count discovery ======================================


@_real
def test_max_averaging_count_reads_configured_n(scope, avg):
    """max_averaging_count() must return the configured N and a real channel
    name -- this is what wait_for_max_sweeps keys off of."""
    nsweeps, ach = avg
    assert ach in scope.channel_names, f"channel {ach!r} not a real channel"
    assert nsweeps == scope.averaging_count(ach), (
        "max_averaging_count disagrees with averaging_count for the same channel"
    )


# === item 9 + 10: the wait completes and ends in STOP =======================


@_real
def test_wait_for_max_sweeps_completes_and_stops(scope, completed_avg):
    """wait_for_max_sweeps() must (a) reach NSweeps within the timeout and
    (b) leave the scope in STOP so the averaged trace is frozen for transfer.
    A timeout here usually means no trigger is reaching the scope (the
    completed_avg fixture skips this whole group in that case)."""
    assert completed_avg["n"] >= completed_avg["nsweeps"], (
        f"completed with {completed_avg['n']} < requested "
        f"{completed_avg['nsweeps']} sweeps"
    )
    mode = scope.set_trigger_mode("")[0:3]  # query-only; codebase compares 3 chars
    DIAG["mode_after_wait"] = mode
    assert mode == "STO", (
        f"TRIG_MODE is {mode!r} after wait_for_max_sweeps, expected STOP -- "
        f"averaged trace is not frozen for transfer"
    )


# === item 11: CLEAR_SWEEPS is ignored in STOP ===============================


@_real
def test_clear_sweeps_in_stop_is_ignored(scope, avg):
    """The core LAPD_DAQ constraint. In STOP the scope ignores CLEAR_SWEEPS, so
    the counter does NOT reset; only after un-freezing (AUTO/NORM) does the
    clear take effect. This test documents the actual hardware behavior; if the
    counter DOES reset in STOP, the un-freeze-before-clear discipline can be
    relaxed."""
    nsweeps, ach = avg
    # This test mutates the sweep counter (clears it in STOP, then in AUTO), so
    # it needs its OWN fresh accumulation -- it cannot share completed_avg.
    timed_out, _ = scope.wait_for_max_sweeps(timeout=WAIT_TIMEOUT)
    if timed_out:
        pytest.skip("could not accumulate sweeps to set up the STOP precondition")
    assert scope.set_trigger_mode("")[0:3] == "STO", "precondition: scope should be in STOP"

    before = scope.sweeps_per_acq(ach)
    scope.clear_sweeps()       # issued WHILE in STOP
    time.sleep(CLEAR_SETTLE_S)
    after_stop = scope.sweeps_per_acq(ach)

    scope.set_trigger_mode("AUTO")  # un-freeze
    scope.clear_sweeps()
    time.sleep(CLEAR_SETTLE_S)
    after_unfreeze = scope.sweeps_per_acq(ach)
    scope.set_trigger_mode("STOP")  # restore frozen state for the next test

    DIAG["sweeps_before_clear"] = before
    DIAG["sweeps_after_clear_in_stop"] = after_stop
    DIAG["sweeps_after_clear_unfrozen"] = after_unfreeze
    # The actionable assertion: clearing after un-freezing genuinely resets.
    # Bound is <=1 (not 0) because AUTO free-runs during the CLEAR_SETTLE_S
    # sleep, so one fresh sweep can land between the clear and the read.
    assert after_unfreeze <= 1, (
        f"counter is {after_unfreeze} after AUTO+CLEAR_SWEEPS, expected <=1 -- "
        f"un-freeze-before-clear did NOT reset the average"
    )
    # Informational: surface whether STOP-clear was honored (not asserted, since
    # either result is a valid hardware fact we want recorded).
    DIAG["clear_in_stop_honored"] = bool(after_stop < before)


# === item 12: back-to-back cycles don't leak stale sweeps ===================


@_real
def test_back_to_back_averages_start_fresh(scope, avg):
    """Two wait_for_max_sweeps() cycles in a row (two simulated shots). The
    second must start from a cleared counter, not accumulate on top of the
    first -- i.e. final sweeps_per_acq ~= N, not ~2N. This exercises the
    per-call AUTO->CLEAR_SWEEPS->NORM that must re-clear even from STOP."""
    nsweeps, ach = avg
    t1, n1 = scope.wait_for_max_sweeps(timeout=WAIT_TIMEOUT)
    if t1:
        pytest.skip(f"first cycle timed out at {n1}/{nsweeps}")
    t2, n2 = scope.wait_for_max_sweeps(timeout=WAIT_TIMEOUT)
    if t2:
        pytest.skip(f"second cycle timed out at {n2}/{nsweeps}")
    DIAG["cycle2_n"] = n2
    # Allow a little overshoot (a sweep or two may land before STOP takes hold),
    # but reject ~2N which would mean the second cycle never cleared. Note this
    # catches only a TOTAL-leak (no clear at all); a partial clear landing
    # between N and 2N would still pass -- inspect cycle2_n in the report.
    assert n2 < 2 * nsweeps, (
        f"second cycle reached {n2} sweeps (~2*{nsweeps}); the counter did NOT "
        f"reset between shots -- stale-sweep leak"
    )


# === item 13: averaged trace reads back as int16 ============================


@_real
def test_averaged_trace_reads_int16(scope, completed_avg):
    """After averaging completes, acquire(raw=True) must return raw int16 (1-D),
    readable with the same path SINGLE uses. raw=False would give scaled floats
    that the int16 cast downstream would zero. Reuses the completed_avg cycle --
    the scope is already frozen in STOP at >= NSweeps."""
    data, _wd = scope.acquire(completed_avg["channel"], raw=True)
    arr = np.asarray(data)
    DIAG["avg_trace_len"] = int(arr.size)
    DIAG["avg_trace_dtype"] = str(arr.dtype)
    assert arr.ndim == 1, "averaged trace should be 1-D"
    assert arr.dtype == np.dtype("<i2"), (
        f"averaged trace dtype {arr.dtype}, expected int16 (<i2)"
    )
    assert arr.min() != arr.max(), (
        "averaged trace is constant -- truncation/scaling/offset corruption"
    )


# === item 14: comm_type round-trips across an averaging cycle ===============


@_real
def test_comm_type_survives_averaging_cycle(scope, completed_avg):
    """wait_for_sweeps writes COMM_FORMAT ...,BYTE and never restores WORD.
    comm_type must read 1 (WORD) BEFORE and AFTER a full averaging cycle; if it
    flips to 0 afterward, every subsequent sequence read halves its sample math
    -- the single state leak that can silently corrupt a mixed run. The BEFORE
    baseline + the cycle come from completed_avg; this test does the AFTER read."""
    before = completed_avg["comm_before"]
    after = scope.translate_wavedesc_bytes(
        scope.acquire_bytes(completed_avg["channel"], seg=0)[1]
    ).comm_type
    DIAG["comm_type_after_avg"] = after
    assert before == 1, f"comm_type started at {before}, expected 1 (WORD)"
    assert after == 1, (
        f"comm_type is {after} (BYTE) after the averaging cycle; "
        f"wait_for_sweeps left COMM_FORMAT as BYTE -- WORD state leak"
    )


# === full report (always passes; run last) ==================================


@_real
def test_zz_report(scope):
    """Prints a complete averaging-cycle report. Paste this whole block back."""
    print("\n" + "=" * 74)
    print("LeCroy AVERAGING-MODE REPORT  (paste this whole block back)")
    print("=" * 74)
    print(f"  SCOPE_IP                  : {SCOPE_IP}")
    try:
        print(f"  IDN                       : {scope.idn_string.strip()!r}")
    except Exception:
        pass
    for k in (
        "channel", "nsweeps",
        "wait1_timed_out", "wait1_n", "mode_after_wait",
        "sweeps_before_clear", "sweeps_after_clear_in_stop",
        "clear_in_stop_honored", "sweeps_after_clear_unfrozen",
        "cycle2_n",
        "avg_trace_len", "avg_trace_dtype",
        "comm_type_before_avg", "comm_type_after_avg",
    ):
        if k in DIAG:
            print(f"  {k:26}: {DIAG[k]}")
    print("=" * 74)

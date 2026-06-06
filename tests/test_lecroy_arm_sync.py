"""Unit tests for the master/slave arm-sync primitives (no hardware).

Covers the INR trigger-ready primitives and the master/slave arm helpers added
to LeCroyScope:
  * read_inr / wait_for_trigger_ready (INR status-register polling),
  * arm_single_and_confirm (slave: arm + confirm ready),
  * arm_master_single (master: arm with a strict SIN check, retry on STOP).

A FakeTransport stands in for LeCroyVICPTransport: it records writes and answers
queries from a scripted/programmable map, so we can model a scope that is/ isn't
trigger-ready and one that fires (STOP) before/after we confirm SIN.
"""

import pytest

from lab_scopes.lecroy.scope import LeCroyScope, INR_TRIGGER_READY


class FakeTransport:
    def __init__(self):
        self.connected = True
        self.timeout = 5.0
        self.chunk_size = 0
        self.writes = []
        # Programmable query answers.
        self.idn = "LeCroy,Fake,0,1"
        self.trig_mode_answers = ["SINGLE"]  # popped per TRIG_MODE? query
        self.inr_answers = [str(INR_TRIGGER_READY)]  # popped per INR? query

    # -- transport surface used by LeCroyScope --
    def open(self):
        self.connected = True

    def close(self):
        self.connected = False

    def write(self, cmd):
        self.writes.append(cmd)

    def query(self, cmd):
        self.writes.append(cmd)
        if cmd == "*IDN?":
            return self.idn
        if cmd == "TRIG_MODE?":
            return self._pop(self.trig_mode_answers, "STOP")
        if cmd == "INR?":
            return self._pop(self.inr_answers, "0")
        if cmd.endswith(":TRACE?"):
            return "OFF"
        if cmd == "CMR?":
            return "0"
        return ""

    @staticmethod
    def _pop(lst, default):
        if not lst:
            return default
        return lst.pop(0) if len(lst) > 1 else lst[0]


def _make_scope(transport):
    # Bypass __init__'s connection/probe logic; wire just what these methods touch.
    scope = LeCroyScope.__new__(LeCroyScope)
    scope.transport = transport
    scope.scope = transport
    scope.verbose = False
    scope.valid_trace_names = ("C1",)
    return scope


def test_read_inr_parses_integer():
    t = FakeTransport()
    t.inr_answers = ["8192"]
    scope = _make_scope(t)
    assert scope.read_inr() == 8192


def test_read_inr_returns_zero_on_garbage():
    t = FakeTransport()
    t.inr_answers = ["OFF"]
    scope = _make_scope(t)
    assert scope.read_inr() == 0


def test_wait_for_trigger_ready_true_when_bit_set():
    t = FakeTransport()
    t.inr_answers = [str(INR_TRIGGER_READY)]
    scope = _make_scope(t)
    assert scope.wait_for_trigger_ready(timeout=0.2) is True


def test_wait_for_trigger_ready_times_out_when_bit_never_set():
    t = FakeTransport()
    t.inr_answers = ["0"]
    scope = _make_scope(t)
    assert scope.wait_for_trigger_ready(timeout=0.1, poll=0.01) is False


def test_wait_for_trigger_ready_ors_across_reads():
    # INR is read-to-clear: the ready bit appears on the 2nd read then is gone.
    t = FakeTransport()
    t.inr_answers = ["0", str(INR_TRIGGER_READY), "0", "0"]

    def query(cmd):
        if cmd == "INR?":
            return t.inr_answers.pop(0) if t.inr_answers else "0"
        return FakeTransport.query(t, cmd)

    t.query = query
    scope = _make_scope(t)
    assert scope.wait_for_trigger_ready(timeout=0.5, poll=0.001) is True


def test_arm_single_and_confirm_returns_channel_and_ready():
    t = FakeTransport()
    t.trig_mode_answers = ["SINGLE"]
    t.inr_answers = [str(INR_TRIGGER_READY)]
    scope = _make_scope(t)
    channel, ready = scope.arm_single_and_confirm(channel="C1", ready_timeout=0.2)
    assert channel == "C1"
    assert ready is True
    assert "CLEAR_SWEEPS" in t.writes
    assert "TRIG_MODE SINGLE" in t.writes


def test_arm_single_and_confirm_not_ready_reports_false():
    t = FakeTransport()
    t.trig_mode_answers = ["SINGLE"]
    t.inr_answers = ["0"]
    scope = _make_scope(t)
    _channel, ready = scope.arm_single_and_confirm(channel="C1", ready_timeout=0.1)
    assert ready is False


def test_arm_master_single_accepts_real_sin():
    t = FakeTransport()
    t.trig_mode_answers = ["SINGLE"]
    scope = _make_scope(t)
    channel = scope.arm_master_single(channel="C1")
    assert channel == "C1"
    assert "TRIG_MODE SINGLE" in t.writes


def test_arm_master_single_arms_exactly_once():
    # The master must be armed exactly once -- re-arming would re-pulse its
    # trigger-out and can double-trigger the slaves. Even when it never reads
    # back SIN, there must be only ONE CLEAR_SWEEPS + ONE TRIG_MODE SINGLE.
    t = FakeTransport()

    def query(cmd):
        if cmd == "TRIG_MODE?":
            return "STOP"
        return FakeTransport.query(t, cmd)

    t.query = query
    scope = _make_scope(t)
    channel = scope.arm_master_single(channel="C1")
    assert channel == "C1"
    assert t.writes.count("CLEAR_SWEEPS") == 1
    assert t.writes.count("TRIG_MODE SINGLE") == 1


def test_arm_master_single_warns_when_not_sin(capsys):
    # If the master does not hold SIN after arming, a warning is printed but the
    # call still returns (best-effort), without re-arming.
    t = FakeTransport()

    def query(cmd):
        if cmd == "TRIG_MODE?":
            return "STOP"
        return FakeTransport.query(t, cmd)

    t.query = query
    scope = _make_scope(t)
    scope.arm_master_single(channel="C1")
    out = capsys.readouterr().out
    assert "did not hold SIN" in out


def test_arm_master_single_no_warning_when_sin(capsys):
    t = FakeTransport()
    t.trig_mode_answers = ["SINGLE"]
    scope = _make_scope(t)
    scope.arm_master_single(channel="C1")
    out = capsys.readouterr().out
    assert "did not hold SIN" not in out


def test_set_trigger_mode_accepts_stop_shortcut():
    # An instant STOP after arming SINGLE is treated as armed-then-fired, so the
    # verify loop breaks immediately instead of spinning all 25 retries.
    t = FakeTransport()
    calls = {"n": 0}

    def query(cmd):
        if cmd == "TRIG_MODE?":
            calls["n"] += 1
            return "STOP"
        return FakeTransport.query(t, cmd)

    t.query = query
    scope = _make_scope(t)
    scope.set_trigger_mode("SINGLE")
    # prev-mode read (1) + first verify poll sees STOP and breaks (1) = 2.
    assert calls["n"] == 2


def test_wait_for_stop_then_complete_requires_both_stop_and_counter():
    # Stage 1 = TRIG_MODE STOP hint; stage 2 = sweep counter >= 1. Both must hold.
    t = FakeTransport()
    scope = _make_scope(t)

    state = {"trig": "SINGLE", "sweeps": 0}
    t.query = lambda cmd: state["trig"] if cmd == "TRIG_MODE?" else ""
    scope._read_sweeps_per_acq = lambda ch: state["sweeps"]

    # Not stopped yet -> times out quickly.
    assert scope.wait_for_stop_then_complete("C1", timeout=0.1, poll=0.01) is False

    # Stopped but counter still 0 (leftover/stale STOP) -> still not complete.
    state["trig"] = "STOP"
    assert scope.wait_for_stop_then_complete("C1", timeout=0.1, poll=0.01) is False

    # Stopped AND a fresh sweep landed -> complete.
    state["sweeps"] = 1
    assert scope.wait_for_stop_then_complete("C1", timeout=0.5, poll=0.01) is True


def test_wait_for_stop_then_complete_waits_for_stop_before_confirming():
    # The counter must only be trusted once the scope is STOPped: a counter that
    # reads >=1 while still SINGLE must NOT be reported complete.
    t = FakeTransport()
    scope = _make_scope(t)

    state = {"trig": "SINGLE"}
    t.query = lambda cmd: state["trig"] if cmd == "TRIG_MODE?" else ""
    # Counter already 1, but we are still armed (SINGLE) -> not complete.
    scope._read_sweeps_per_acq = lambda ch: 1

    assert scope.wait_for_stop_then_complete("C1", timeout=0.1, poll=0.01) is False
    state["trig"] = "STOP"
    assert scope.wait_for_stop_then_complete("C1", timeout=0.5, poll=0.01) is True

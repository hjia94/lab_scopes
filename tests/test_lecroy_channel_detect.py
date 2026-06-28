"""Unit tests for LeCroy channel-count detection and trace discovery (no hardware).

Covers the init-time logic:
  * _detect_channel_count -- authoritative ``Acquisition.Channels`` VBS query,
    with a clean-probe (``C1..C8`` + ``*CLS`` per probe) fallback for firmware
    that does not answer it,
  * channel-count-bounded trace discovery (valid_trace_names),
  * displayed_channels / validate_channel honoring the detected count,
  * robustness: a connection drop during detection is fatal; an unavailable /
    garbled channel-count reply degrades safely (probe fallback, else 4).

The ``*IDN?`` model string is NOT used for detection -- some 4-channel models
carry IDNs that look 8-channel, so it is unreliable -- and these tests pin that
the count comes from ``Acquisition.Channels`` instead.

An error-aware FakeTransport stands in for LeCroyVICPTransport. Unlike the
arm-sync fake (which answers CMR? == 0 unconditionally), this one models per
-channel validity: C5-C8 are rejected on a 4-channel scope (CMR? != 0, like real
hardware), and it can carry a *pre-latched* command error to reproduce the
read-to-clear hazard that previously mis-detected scopes.
"""

import pytest

from lab_scopes.errors import ScopeConnectionError, ScopeTimeoutError
from lab_scopes.lecroy.scope import LeCroyScope


class FakeTransport:
    """Programmable VICP stand-in that models a 2/4/8-channel scope.

    Parameters
    ----------
    idn : str
        Reply to ``*IDN?`` (recorded as metadata; NOT used for detection).
    n_channels : int
        Channels the modeled scope physically has (2/4/8). Also the value
        reported by ``VBS? "return=app.Acquisition.Channels"`` unless
        ``channels_reply`` overrides it. A ``Cn:TRACE?`` for ``n`` beyond this
        sets the error register so the following ``CMR?`` returns non-zero --
        mirroring how real hardware rejects an invalid channel. ``displayed``
        channels (below) report ``ON``.
    displayed : tuple[str, ...]
        Channel names whose ``:TRACE?`` should report ``ON``.
    cmr_reply : str | None
        Override for every ``CMR?`` reply (used to inject a non-numeric value).
    channels_reply : str | None
        Override for the ``Acquisition.Channels`` VBS reply. Set to ``""`` or a
        non-numeric value to force the probe fallback; default reports
        ``n_channels``.
    prelatched_err : bool
        If true, the command-error register starts set (1), as if an earlier
        command errored and its ``CMR?`` read never ran. Detection must not let
        this stale error corrupt the channel count.
    raise_on : str | None
        Substring; if a queried command contains it, raise
        ``ScopeConnectionError`` (models a link drop mid-detection).
    invalid_trace_times_out : bool
        If true, a ``Cn:TRACE?`` beyond the channel count behaves like real
        4-channel LeCroy hardware: the error register is set and **no reply is
        sent** (the read times out -> ``ScopeTimeoutError``), instead of the
        default polite ``OFF`` reply.
    """

    def __init__(self, idn="LeCroy,WAVERUNNER 9254,LCRY,1.0", n_channels=4,
                 displayed=(), cmr_reply=None, channels_reply=None,
                 prelatched_err=False, raise_on=None,
                 invalid_trace_times_out=False):
        self.connected = False
        self.timeout = 5.0
        self.chunk_size = 0
        self.idn = idn
        self.n_channels = n_channels
        self.displayed = tuple(displayed)
        self.cmr_reply = cmr_reply
        self.channels_reply = channels_reply
        self.raise_on = raise_on
        self.invalid_trace_times_out = invalid_trace_times_out
        self.writes = []
        self._err = 1 if prelatched_err else 0  # command-error register
        self._last_trace = ""  # channel of the most recent :TRACE? (for CMR?)

    # -- transport surface used by LeCroyScope --
    def open(self):
        self.connected = True

    def close(self):
        self.connected = False

    def write(self, cmd):
        self.writes.append(cmd)
        if cmd == "*CLS":
            self._err = 0  # IEEE-488.2 clear-status resets the error register

    def query(self, cmd):
        self.writes.append(cmd)
        if self.raise_on is not None and self.raise_on in cmd:
            raise ScopeConnectionError(f"simulated drop on {cmd!r}")
        if cmd == "*IDN?":
            return self.idn
        if "Acquisition.Channels" in cmd:
            return self.channels_reply if self.channels_reply is not None else str(self.n_channels)
        if cmd.endswith(":TRACE?"):
            ch = cmd.split(":")[0]
            self._last_trace = ch
            # An invalid channel (beyond this scope's count) sets the error
            # register, exactly like real hardware rejecting Cn.
            if ch.startswith("C") and ch[1:].isdigit() and int(ch[1:]) > self.n_channels:
                self._err = 1
                if self.invalid_trace_times_out:
                    raise ScopeTimeoutError(f"no reply to {cmd!r}")
            return "ON" if ch in self.displayed else "OFF"
        if cmd == "CMR?":
            if self.cmr_reply is not None:
                return self.cmr_reply
            err = self._err
            self._err = 0  # CMR? is read-to-clear
            return str(err)
        return ""


def _make(idn="LeCroy,WAVERUNNER 9254,LCRY,1.0", n_channels=4, **kw):
    t = FakeTransport(idn=idn, n_channels=n_channels, **kw)
    scope = LeCroyScope("1.2.3.4", verbose=False, transport=t)
    return scope, t


# -- channel-count detection ------------------------------------------------- #

def test_detect_uses_acquisition_channels_4ch():
    # The count comes straight from Acquisition.Channels -- no C5 probe needed.
    scope, t = _make(n_channels=4)
    assert scope.n_channels == 4
    assert scope.channel_names == ("C1", "C2", "C3", "C4")
    assert scope.valid_trace_names == ("C1", "C2", "C3", "C4")
    # Authoritative query was used; detection never probed C5+ to decide.
    assert any("Acquisition.Channels" in w for w in t.writes)
    assert "C5:TRACE?" not in t.writes


def test_detect_uses_acquisition_channels_8ch():
    # An 8-channel scope reports 8 via Acquisition.Channels regardless of model.
    scope, _ = _make(idn="LeCroy,MYSTERY MODEL,LCRY,1.0", n_channels=8)
    assert scope.n_channels == 8
    assert scope.channel_names == tuple(f"C{i}" for i in range(1, 9))
    assert scope.valid_trace_names == tuple(f"C{i}" for i in range(1, 9))


def test_detect_ignores_misleading_idn():
    # Regression: a 4-channel scope whose *IDN? looks 8-channel must still be
    # detected as 4 -- the model string is never used for the count.
    scope, _ = _make(idn="LeCroy,WAVERUNNER 8000HD,LCRY,1.0", n_channels=4)
    assert scope.n_channels == 4
    assert scope.valid_trace_names == ("C1", "C2", "C3", "C4")


def test_detect_8ch_survives_prelatched_cmr_error():
    # Regression for the C5-C8 data loss: a stale command error latched before
    # detection (e.g. by COMM_FORMAT) must NOT make a real 8-channel scope read
    # as 4. Acquisition.Channels is authoritative and immune to CMR state.
    scope, _ = _make(idn="LeCroy,MYSTERY MODEL,LCRY,1.0", n_channels=8,
                     prelatched_err=True)
    assert scope.n_channels == 8
    assert scope.valid_trace_names == tuple(f"C{i}" for i in range(1, 9))


def test_detect_falls_back_to_probe_when_channels_unavailable():
    # Older firmware that does not answer Acquisition.Channels -> clean-probe
    # C1..C8, taking the highest contiguous accepted channel.
    scope, t = _make(n_channels=8, channels_reply="")
    assert scope.n_channels == 8
    assert scope.valid_trace_names == tuple(f"C{i}" for i in range(1, 9))
    # The probe path was actually exercised.
    assert "C5:TRACE?" in t.writes


def test_probe_fallback_4ch_survives_prelatched_error():
    # Even on the probe fallback, a pre-latched error must not corrupt the
    # count: each probe is *CLS-cleared first, so C1 is read cleanly and a
    # 4-channel scope is detected as 4 (not 0/3).
    scope, _ = _make(n_channels=4, channels_reply="", prelatched_err=True)
    assert scope.n_channels == 4
    assert scope.valid_trace_names == ("C1", "C2", "C3", "C4")


def test_detect_connection_drop_propagates():
    # A genuine link drop during detection must fail init loudly, not be
    # silently swallowed into a mis-detection.
    with pytest.raises(ScopeConnectionError):
        _make(n_channels=4, raise_on="Acquisition.Channels")


def test_discovery_preclear_protects_c1_from_prelatched_error(monkeypatch):
    # Defense in depth: even if some pre-discovery traffic leaves an error
    # latched without cleaning up after itself, the discovery loop's own
    # pre-clear must keep C1 from being blamed for it.
    def latching_detect(self):
        self.scope._err = 1  # latched error, no cleanup
        return 4

    monkeypatch.setattr(LeCroyScope, "_detect_channel_count", latching_detect)
    scope, _ = _make(n_channels=4)
    assert scope.valid_trace_names == ("C1", "C2", "C3", "C4")


# -- discovery / validation honoring the detected count ---------------------- #

def test_validate_channel_bounds_4ch():
    scope, _ = _make(n_channels=4)
    assert scope.validate_channel("C4") == "C4"
    assert scope.validate_channel(3) == "C3"
    with pytest.raises(RuntimeError):
        scope.validate_channel("C5")
    with pytest.raises(RuntimeError):
        scope.validate_channel(5)


def test_validate_channel_bounds_8ch():
    scope, _ = _make(n_channels=8)
    assert scope.validate_channel("C8") == "C8"
    assert scope.validate_channel(7) == "C7"


def test_displayed_channels_only_lists_on_channels():
    scope, _ = _make(n_channels=4, displayed=("C1", "C3"))
    assert scope.displayed_channels() == ("C1", "C3")


def test_displayed_channels_skips_garbled_reply():
    # A garbled/timed-out :TRACE? for one channel is skipped, not fatal: the
    # remaining channels are still scanned.
    scope, t = _make(n_channels=4, displayed=("C1", "C2", "C4"))

    real_query = t.query

    def flaky_query(cmd):
        if cmd == "C2:TRACE?":
            raise ValueError("garbled reply")
        return real_query(cmd)

    t.query = flaky_query
    # C2 raises and is skipped; C1 and C4 still reported.
    assert scope.displayed_channels() == ("C1", "C4")


def test_displayed_channels_connection_drop_propagates():
    scope, t = _make(n_channels=4, displayed=("C1",))

    def drop_query(cmd):
        if cmd.endswith(":TRACE?"):
            raise ScopeConnectionError("link gone")
        return FakeTransport.query(t, cmd)

    t.query = drop_query
    with pytest.raises(ScopeConnectionError):
        scope.displayed_channels()

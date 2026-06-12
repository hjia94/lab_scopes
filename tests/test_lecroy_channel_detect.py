"""Unit tests for LeCroy 4-vs-8 channel detection and trace discovery (no hardware).

Covers the init-time logic added on feature/8-channel-support:
  * _detect_channel_count -- *IDN? marker fast path + C5:TRACE?/CMR? fallback,
  * channel-count-bounded trace discovery (valid_trace_names),
  * displayed_channels / validate_channel honoring the detected count,
  * robustness: a connection drop during detection is fatal; an ambiguous reply
    (timeout / non-numeric CMR?) degrades safely to 4 channels.

An error-aware FakeTransport stands in for LeCroyVICPTransport. Unlike the
arm-sync fake (which answers CMR? == 0 unconditionally), this one models per
-channel validity: C5-C8 are rejected on a 4-channel scope (CMR? != 0, like real
hardware), so the 4-vs-8 boundary can actually be exercised.
"""

import pytest

from lab_scopes.errors import ScopeConnectionError, ScopeTimeoutError
from lab_scopes.lecroy.scope import LeCroyScope


class FakeTransport:
    """Programmable VICP stand-in that models a 4- or 8-channel scope.

    Parameters
    ----------
    idn : str
        Reply to ``*IDN?``.
    n_channels : int
        Channels the modeled scope physically has (4 or 8). A ``Cn:TRACE?`` for
        ``n`` beyond this sets the error register so the following ``CMR?``
        returns non-zero -- mirroring how real hardware rejects an invalid
        channel. ``displayed`` channels (below) report ``ON``.
    displayed : tuple[str, ...]
        Channel names whose ``:TRACE?`` should report ``ON``.
    cmr_reply : str | None
        Override for every ``CMR?`` reply (used to inject a non-numeric value).
    raise_on : str | None
        Substring; if a queried command contains it, raise
        ``ScopeConnectionError`` (models a link drop mid-detection).
    invalid_trace_times_out : bool
        If true, a ``Cn:TRACE?`` beyond the channel count behaves like real
        4-channel LeCroy hardware: the error register is set and **no reply is
        sent** (the read times out -> ``ScopeTimeoutError``), instead of the
        default polite ``OFF`` reply. This is the only path where the latched
        error survives into trace discovery.
    """

    def __init__(self, idn="LeCroy,WAVERUNNER 9254,LCRY,1.0", n_channels=4,
                 displayed=(), cmr_reply=None, raise_on=None,
                 invalid_trace_times_out=False):
        self.connected = False
        self.timeout = 5.0
        self.chunk_size = 0
        self.idn = idn
        self.n_channels = n_channels
        self.displayed = tuple(displayed)
        self.cmr_reply = cmr_reply
        self.raise_on = raise_on
        self.invalid_trace_times_out = invalid_trace_times_out
        self.writes = []
        self._err = 0          # error register; set by an invalid :TRACE?
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

def test_detect_4ch_unknown_model_via_c5_probe():
    # Unknown model + C5 rejected (4-ch hardware) -> 4 channels.
    scope, t = _make(idn="LeCroy,WAVERUNNER 9254,LCRY,1.0", n_channels=4)
    assert scope.n_channels == 4
    assert scope.channel_names == ("C1", "C2", "C3", "C4")
    assert scope.valid_trace_names == ("C1", "C2", "C3", "C4")
    # The boundary probe was issued exactly once, and discovery never probed C5+.
    assert t.writes.count("C5:TRACE?") == 1
    assert "C6:TRACE?" not in t.writes


def test_detect_8ch_via_idn_marker_skips_probe():
    # A known 8-ch marker decides without any C5 probe (zero extra round-trips).
    scope, t = _make(idn="LeCroy,WAVERUNNER 8000HD,LCRY,1.0", n_channels=8)
    assert scope.n_channels == 8
    assert scope.channel_names == tuple(f"C{i}" for i in range(1, 9))
    # Detection issued no boundary probe; C5 only appears from discovery.
    # (Discovery probes C1..C8, so C5:TRACE? is present, but CMR? for it == 0.)
    assert scope.valid_trace_names == tuple(f"C{i}" for i in range(1, 9))


def test_detect_8ch_via_c5_probe_fallback():
    # Unknown model but real 8-ch hardware -> C5 probe accepts -> 8 channels.
    scope, t = _make(idn="LeCroy,MYSTERY MODEL,LCRY,1.0", n_channels=8)
    assert scope.n_channels == 8
    assert scope.valid_trace_names == tuple(f"C{i}" for i in range(1, 9))


def test_detect_defaults_to_4_on_nonnumeric_cmr():
    # A non-numeric CMR? after the C5 probe is ambiguous -> safe default of 4
    # (must NOT be mistaken for "0" == accepted).
    scope, _ = _make(idn="LeCroy,ODD FIRMWARE,LCRY,1.0", n_channels=8,
                     cmr_reply="garbage")
    assert scope.n_channels == 4


def test_detect_connection_drop_propagates():
    # A genuine link drop during the C5 probe must fail init loudly, not be
    # silently swallowed into a 4-channel mis-detection.
    with pytest.raises(ScopeConnectionError):
        _make(idn="LeCroy,UNKNOWN,LCRY,1.0", n_channels=4, raise_on="C5:TRACE?")


def test_c5_probe_timeout_does_not_drop_c1():
    # Regression: on real 4-channel hardware the C5 detection probe gets no
    # reply (timeout) and leaves the command-error bit latched in CMR. That
    # stale error must not be blamed on the first discovery candidate (C1),
    # which would silently drop the channel from every acquisition.
    scope, _ = _make(idn="LeCroy,WAVERUNNER 9254,LCRY,1.0", n_channels=4,
                     invalid_trace_times_out=True)
    assert scope.n_channels == 4
    assert scope.valid_trace_names == ("C1", "C2", "C3", "C4")


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
    scope, _ = _make(idn="LeCroy,8000HD,LCRY,1.0", n_channels=8)
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

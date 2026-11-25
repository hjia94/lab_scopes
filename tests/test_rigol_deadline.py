"""Hardware-free tests for the Rigol DHO800 deadline and error handling (docs/rigol_timeout_plan.md).

A loopback fake scope (127.0.0.1, one server thread) answers scripted replies and records
every command line with its arrival time. Run on Windows; the real scope is bench-tested on
Linux (tests/test_rigol_scope_real.py).

    pytest tests/test_rigol_deadline.py -v
"""

import socket
import threading
import time

import numpy as np
import pytest

from lab_scopes.errors import ScopeConnectionError, ScopeTimeoutError
from lab_scopes.rigol import RigolDHO800, RigolScopeError
from lab_scopes.transports import Deadline

IDN = b"RIGOL TECHNOLOGIES,DHO804,DHO8A000000001,00.01.05\n"
SLACK_S = 0.2  # Windows timer resolution plus thread scheduling
CLOSE = object()  # a script callable returns this to drop the connection


class FakeRigol:
    """One-connection loopback scope.

    ``script`` maps a received command line to bytes (sent as is) or to a callable(conn)
    run in the server thread; a line with no entry, or None, gets no reply.
    """

    def __init__(self, script=None):
        self.script = {"*IDN?": IDN, **(script or {})}
        self.received = []  # (monotonic arrival time, line)
        self.closed = threading.Event()  # set once the client connection has ended
        self._server = socket.create_server(("127.0.0.1", 0))
        self.port = self._server.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def lines(self):
        return [line for _, line in self.received]

    def close(self):
        self._server.close()

    def _serve(self):
        try:
            conn, _ = self._server.accept()
        except OSError:
            return
        try:
            with conn:
                self._handle(conn)
        finally:
            self.closed.set()

    def _handle(self, conn):
        buf = b""
        while True:
            try:
                data = conn.recv(4096)
            except OSError:
                return
            if not data:
                return
            buf += data
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                line = raw.decode("ascii")
                self.received.append((time.monotonic(), line))
                reply = self.script.get(line)
                try:
                    if callable(reply):
                        if reply(conn) is CLOSE:
                            return
                    elif reply is not None:
                        conn.sendall(reply)
                except OSError:
                    return


def stall(seconds=3.0):
    return lambda conn: time.sleep(seconds)


def send_then(data, action):
    def run(conn):
        conn.sendall(data)
        return action(conn) if callable(action) else action
    return run


def trickle(header, n_bytes, interval):
    def run(conn):
        conn.sendall(header)
        for _ in range(n_bytes):
            time.sleep(interval)
            conn.sendall(b"\x00")
    return run


@pytest.fixture
def fake():
    servers = []

    def make(script=None):
        server = FakeRigol(script)
        servers.append(server)
        return server

    yield make
    for server in servers:
        server.close()


def connect(server, deadline=None):
    return RigolDHO800("127.0.0.1", port=server.port, timeout=2.0, verbose=False, deadline=deadline)


def wait_for(predicate, timeout=1.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


# -- the read path: commands sent and values returned ------------------------------ #

# WORD codes as the DHO804 sends them: around YREFerence 32768 (about 15,600..50,500 on
# the bench), including values whose little-endian bytes contain 0xFF (IAC to telnetlib).
CODES = np.array([15600, 32767, 32768, 32769, 33023, 40000, 49919, 50500, 25000, 32511], dtype="<u2")
CODES2 = np.array([50500, 49919, 33023, 32768, 15600, 16383, 20000, 30000, 32767, 45000], dtype="<u2")

CAL1 = {  # channel 1: the separate-query replies and the PREamble fields agree
    "x_increment": "1.600000E-07", "x_origin": "-8.000000E-02", "x_reference": "0",
    "y_increment": "1.333333E-04", "y_origin": "5100", "y_reference": "32768",
}
CAL2 = {
    "x_increment": "1.600000E-07", "x_origin": "-8.000000E-02", "x_reference": "0",
    "y_increment": "2.666667E-04", "y_origin": "-1200", "y_reference": "32768",
}
CAL_QUERIES = {
    "x_increment": ":WAVeform:XINCrement?", "x_origin": ":WAVeform:XORigin?",
    "x_reference": ":WAVeform:XREFerence?", "y_increment": ":WAVeform:YINCrement?",
    "y_origin": ":WAVeform:YORigin?", "y_reference": ":WAVeform:YREFerence?",
}


def preamble(cal, fmt_code=1, points=10):
    fields = [str(fmt_code), "1", str(points), "1"] + [cal[k] for k in CAL_QUERIES]
    return (",".join(fields) + "\n").encode()


def block(codes):
    data = codes.tobytes()
    return b"#9%09d" % len(data) + data + b"\n"


def scope_script(preamble_override=None):
    """Replies that follow the current :WAVeform:SOURce, as on the real scope."""
    per_source = {
        "CHANnel1": {"cal": CAL1, "codes": CODES},
        "CHANnel2": {"cal": CAL2, "codes": CODES2},
    }
    state = {"src": "CHANnel1"}

    def select(src):
        def run(conn):
            state["src"] = src
        return run

    def reply(make):
        return lambda conn: conn.sendall(make(per_source[state["src"]]))

    script = {
        ":TRIGger:STATus?": b"STOP\n",
        ":ACQuire:MDEPth?": b"1.0000E+01\n",
        ":TIMebase:MAIN:SCALe?": b"1.000000E-06\n",
        ":TIMebase:MAIN:OFFSet?": b"0.000000E+00\n",
        ":WAVeform:SOURce CHANnel1": select("CHANnel1"),
        ":WAVeform:SOURce CHANnel2": select("CHANnel2"),
        ":WAVeform:PREamble?": preamble_override or reply(lambda s: preamble(s["cal"])),
        ":WAVeform:DATA?": reply(lambda s: block(s["codes"])),
        ":CHANnel1:SCALe?": b"5.000000E-01\n",
        ":CHANnel1:OFFSet?": b"0.000000E+00\n",
        ":CHANnel2:SCALe?": b"1.000000E+00\n",
        ":CHANnel2:OFFSet?": b"-2.000000E-01\n",
    }
    for key, query in CAL_QUERIES.items():
        script[query] = reply(lambda s, key=key: (s["cal"][key] + "\n").encode())
    return script


SHOT_LINES = [  # once per read_channels call
    ":TRIGger:STATus?",
    ":ACQuire:MDEPth?",
    ":TIMebase:MAIN:SCALe?",
    ":TIMebase:MAIN:OFFSet?",
]


def channel_lines(n, calibration=(":WAVeform:PREamble?",)):
    return [
        f":WAVeform:SOURce CHANnel{n}",
        ":WAVeform:MODE MAXimum",
        ":WAVeform:FORMat WORD",
        *calibration,
        ":WAVeform:STARt 1",
        ":WAVeform:STOP 10",
        ":WAVeform:DATA?",
        f":CHANnel{n}:SCALe?",
        f":CHANnel{n}:OFFSet?",
    ]


@pytest.fixture
def settle_sleeps(monkeypatch):
    """Records every settle delay command() takes (and still sleeps it)."""
    import lab_scopes.transports.rigol_functions as rf

    calls = []
    real = rf.sleep

    def record(deadline, seconds):
        calls.append(seconds)
        real(deadline, seconds)

    monkeypatch.setattr(rf, "sleep", record)
    return calls


def expected_voltage(codes, cal):
    return (codes.astype(np.float64) - float(cal["y_origin"]) - float(cal["y_reference"])) * float(cal["y_increment"])


@pytest.mark.parametrize("deadline_s", [None, 10.0])
def test_read_channel_sends_expected_lines_and_decodes(fake, settle_sleeps, deadline_s):
    server = fake(scope_script())
    deadline = None if deadline_s is None else Deadline(deadline_s)
    with connect(server, deadline) as scope:
        wf = scope.read_channel("C1", fmt="WORD")
        assert scope.connected

    assert server.lines() == ["*IDN?", *SHOT_LINES, *channel_lines(1)]
    assert settle_sleeps == []  # no settle delay after :WAVeform:SOURce/MODE/FORMat

    assert wf.channel == "CHANnel1"
    assert wf.raw.dtype == np.dtype("<u2")
    np.testing.assert_array_equal(wf.raw, CODES)
    np.testing.assert_allclose(wf.voltage, expected_voltage(CODES, CAL1))
    np.testing.assert_allclose(wf.time, -8e-2 + np.arange(10) * 1.6e-7)
    assert wf.metadata == {
        "format": 1, "type": 1, "points": 10, "count": 1,
        "x_increment": 1.6e-7, "x_origin": -8e-2, "x_reference": 0.0,
        "y_increment": 1.333333e-4, "y_origin": 5100.0, "y_reference": 32768.0,
        "vertical_scale": 0.5, "vertical_offset": 0.0,
        "timebase_scale": 1e-6, "timebase_offset": 0.0,
    }


def test_read_channels_sends_shot_queries_once(fake, settle_sleeps):
    server = fake(scope_script())
    with connect(server) as scope:
        waves = scope.read_channels(["C1", "C2"])

    assert server.lines() == ["*IDN?", *SHOT_LINES, *channel_lines(1), *channel_lines(2)]
    assert settle_sleeps == []
    assert list(waves) == ["C1", "C2"]  # keyed as passed, in order
    np.testing.assert_array_equal(waves["C1"].raw, CODES)
    np.testing.assert_array_equal(waves["C2"].raw, CODES2)
    np.testing.assert_allclose(waves["C2"].voltage, expected_voltage(CODES2, CAL2))
    assert waves["C2"].metadata["y_origin"] == -1200.0
    assert waves["C2"].metadata["vertical_offset"] == -0.2


def assert_same_waveform(a, b):
    assert a.channel == b.channel
    assert a.raw.dtype == b.raw.dtype
    assert a.raw.tobytes() == b.raw.tobytes()
    assert a.voltage.tobytes() == b.voltage.tobytes()
    assert a.time.tobytes() == b.time.tobytes()
    assert a.metadata == b.metadata
    assert list(a.metadata) == list(b.metadata)


def test_read_channels_equals_two_read_channel_calls(fake):
    server = fake(scope_script())
    with connect(server) as scope:
        together = scope.read_channels([1, 2])
        one = scope.read_channel(1)
        two = scope.read_channel(2)
    assert_same_waveform(together[1], one)
    assert_same_waveform(together[2], two)


def test_preamble_and_separate_queries_give_identical_results(fake):
    server = fake(scope_script())
    with connect(server) as scope:
        from_preamble = scope.read_channels(["C1", "C2"])
        n_before = len(server.lines())
        scope.calibration_from_preamble = False
        from_queries = scope.read_channels(["C1", "C2"])
    cal = tuple(CAL_QUERIES.values())
    assert server.lines()[n_before:] == [*SHOT_LINES, *channel_lines(1, cal), *channel_lines(2, cal)]
    for ch in ("C1", "C2"):
        assert_same_waveform(from_preamble[ch], from_queries[ch])


def test_preamble_with_wrong_format_raises(fake):
    bad = lambda conn: conn.sendall(preamble(CAL1, fmt_code=0))  # BYTE while WORD was set
    server = fake(scope_script(preamble_override=bad))
    with connect(server) as scope:
        with pytest.raises(RigolScopeError, match="format"):
            scope.read_channel("C1")


@pytest.mark.parametrize("reply", [b"1,1,10,1,1e-7\n", b"1,1,10,1,1e-7,0,0,abc,0,32768\n"])
def test_malformed_preamble_raises(fake, reply):
    server = fake(scope_script(preamble_override=reply))
    with connect(server) as scope:
        with pytest.raises(RigolScopeError, match="PREamble"):
            scope.read_channel("C1")


def test_read_channels_rejects_duplicates_before_sending(fake):
    server = fake(scope_script())
    with connect(server) as scope:
        with pytest.raises(ValueError):
            scope.read_channels(["C1", 1])
        with pytest.raises(ValueError):
            scope.read_channels([])
    assert server.lines() == ["*IDN?"]


# -- deadline bounds every wait ------------------------------------------------------ #

def test_delayed_reply_times_out_at_deadline_and_closes(fake):
    server = fake({":TRIGger:STATus?": stall()})
    scope = connect(server)
    scope.deadline = Deadline(0.5)
    t0 = time.monotonic()
    with pytest.raises(ScopeTimeoutError):
        scope.trigger_status()
    assert time.monotonic() - t0 < 0.5 + SLACK_S
    assert not scope.connected
    with pytest.raises(ScopeConnectionError):
        scope.trigger_status()


def test_block_header_then_stall_times_out_and_closes(fake):
    server = fake({":WAVeform:DATA?": send_then(b"#9000001000" + bytes(10), stall())})
    scope = connect(server, Deadline(0.5))
    t0 = time.monotonic()
    with pytest.raises(ScopeTimeoutError):
        scope._read_block(":WAVeform:DATA?", timeout=15)
    assert time.monotonic() - t0 < 0.5 + SLACK_S
    with pytest.raises(ScopeConnectionError):
        scope.trigger_status()


def test_trickle_cannot_outlast_deadline(fake):
    # 1 byte per 0.1 s would keep a per-recv timeout alive for ~100 s.
    server = fake({":WAVeform:DATA?": trickle(b"#9000001000", 1000, 0.1)})
    scope = connect(server, Deadline(0.6))
    t0 = time.monotonic()
    with pytest.raises(ScopeTimeoutError):
        scope._read_block(":WAVeform:DATA?", timeout=15)
    assert time.monotonic() - t0 < 0.6 + SLACK_S


def test_settle_delay_is_never_shortened(fake):
    server = fake()
    scope = connect(server)
    scope.deadline = Deadline(0.03)  # less than the 50 ms settle delay
    t0 = time.monotonic()
    with pytest.raises(ScopeTimeoutError):
        scope.set_sweep("NORMal")  # :TRIGger: commands keep their settle delay
    assert time.monotonic() - t0 < 0.05  # raised instead of sleeping a shorter delay
    assert wait_for(lambda: ":TRIGger:SWEep NORMal" in server.lines())
    with pytest.raises(ScopeConnectionError):
        scope._write(":WAVeform:MODE MAXimum")
    assert server.closed.wait(1.0)  # set after every line that arrived is recorded
    assert ":WAVeform:MODE MAXimum" not in server.lines()


def test_no_time_left_sends_nothing(fake):
    server = fake()
    scope = connect(server)
    scope.deadline = Deadline(0.0)
    with pytest.raises(ScopeTimeoutError):
        scope.stop()
    assert server.closed.wait(1.0)
    assert server.lines() == ["*IDN?"]


def test_connect_is_bounded_by_deadline():
    probe = socket.create_server(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()  # nothing listens here now
    t0 = time.monotonic()
    with pytest.raises(ScopeConnectionError):
        RigolDHO800("127.0.0.1", port=port, timeout=5.0, verbose=False, deadline=Deadline(0.5))
    assert time.monotonic() - t0 < 0.5 + SLACK_S


def test_idn_failure_closes_socket(fake):
    server = fake({"*IDN?": None})
    with pytest.raises(ScopeTimeoutError):
        connect(server, Deadline(0.5))
    assert server.closed.wait(1.0)


# -- failures surface at once and never as data ------------------------------------ #

def test_peer_close_mid_block_fails_fast(fake):
    server = fake({":WAVeform:DATA?": send_then(b"#9000001000" + bytes(5), CLOSE)})
    scope = connect(server)  # no deadline: only EOF detection can make this fast
    t0 = time.monotonic()
    with pytest.raises(ScopeConnectionError):
        scope._read_block(":WAVeform:DATA?", timeout=15)
    assert time.monotonic() - t0 < 1.0


def test_peer_close_before_text_reply_fails_fast(fake):
    server = fake({":TRIGger:STATus?": lambda conn: CLOSE})
    scope = connect(server)
    t0 = time.monotonic()
    with pytest.raises(ScopeConnectionError):
        scope.trigger_status()
    assert time.monotonic() - t0 < 1.0


def test_partial_text_reply_is_an_error_not_a_value(fake):
    # v0.4.0 returned "1.0000E+0" here, i.e. a depth of 1 instead of 10.
    server = fake({":ACQuire:MDEPth?": send_then(b"1.0000E+0", stall())})
    scope = connect(server, Deadline(0.5))
    with pytest.raises(ScopeTimeoutError):
        scope.memory_depth()


def test_query_default_applies_to_unparseable_reply_only(fake):
    server = fake({":TIMebase:MAIN:OFFSet?": b"abc\n", ":TIMebase:MAIN:SCALe?": stall()})
    scope = connect(server)
    assert scope.timebase_offset() == 0.0
    assert scope.connected
    scope.deadline = Deadline(0.5)
    with pytest.raises(ScopeTimeoutError):  # v0.4.0 returned the default 0.0 here
        scope.timebase_scale()

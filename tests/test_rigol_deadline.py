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
from lab_scopes.rigol import RigolDHO800
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


# -- invariants: the normal path is byte-for-byte v0.4.0 ---------------------------- #

# 12-bit codes, including values whose little-endian bytes contain 0xFF (IAC to telnetlib).
CODES = np.array([0, 1, 255, 256, 2047, 2048, 2049, 3840, 4095, 1024], dtype="<u2")

READ_SCRIPT = {
    ":TRIGger:STATus?": b"STOP\n",
    ":ACQuire:MDEPth?": b"1.0000E+01\n",
    ":WAVeform:XINCrement?": b"1.000000E-09\n",
    ":WAVeform:XORigin?": b"-5.000000E-09\n",
    ":WAVeform:XREFerence?": b"0\n",
    ":WAVeform:YINCrement?": b"1.000000E-03\n",
    ":WAVeform:YORigin?": b"0\n",
    ":WAVeform:YREFerence?": b"2048\n",
    ":WAVeform:DATA?": b"#9000000020" + CODES.tobytes() + b"\n",
    ":CHANnel1:SCALe?": b"5.000000E-01\n",
    ":CHANnel1:OFFSet?": b"0.000000E+00\n",
    ":TIMebase:MAIN:SCALe?": b"1.000000E-06\n",
    ":TIMebase:MAIN:OFFSet?": b"0.000000E+00\n",
}

# The sequence v0.4.0 sends for read_channel('C1', fmt='WORD') on a 10-point record.
V040_READ_LINES = [
    "*IDN?",
    ":TRIGger:STATus?",
    ":WAVeform:SOURce CHANnel1",
    ":WAVeform:MODE MAXimum",
    ":WAVeform:FORMat WORD",
    ":ACQuire:MDEPth?",
    ":WAVeform:XINCrement?",
    ":WAVeform:XORigin?",
    ":WAVeform:XREFerence?",
    ":WAVeform:YINCrement?",
    ":WAVeform:YORigin?",
    ":WAVeform:YREFerence?",
    ":WAVeform:STARt 1",
    ":WAVeform:STOP 10",
    ":WAVeform:DATA?",
    ":CHANnel1:SCALe?",
    ":CHANnel1:OFFSet?",
    ":TIMebase:MAIN:SCALe?",
    ":TIMebase:MAIN:OFFSet?",
]
SETTLED = (":WAVeform:SOURce CHANnel1", ":WAVeform:MODE MAXimum", ":WAVeform:FORMat WORD")


@pytest.mark.parametrize("deadline_s", [None, 10.0])
def test_read_channel_sends_v040_lines_and_decodes(fake, deadline_s):
    server = fake(READ_SCRIPT)
    deadline = None if deadline_s is None else Deadline(deadline_s)
    with connect(server, deadline) as scope:
        wf = scope.read_channel("C1", fmt="WORD")
        assert scope.connected

    assert server.lines() == V040_READ_LINES
    for (t, line), (t_next, _) in zip(server.received, server.received[1:]):
        if line in SETTLED:  # the 50 ms settle delay after each is kept in full
            assert t_next - t >= 0.045

    assert wf.raw.dtype == np.dtype("<u2")
    np.testing.assert_array_equal(wf.raw, CODES)
    np.testing.assert_allclose(wf.voltage, (CODES.astype(np.float64) - 0 - 2048) * 1e-3)
    np.testing.assert_allclose(wf.time, -5e-9 + np.arange(10) * 1e-9)
    assert wf.metadata["points"] == 10
    assert wf.metadata["y_reference"] == 2048.0
    assert wf.metadata["vertical_scale"] == 0.5


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
        scope._write(":WAVeform:SOURce CHANnel1")
    assert time.monotonic() - t0 < 0.05  # raised instead of sleeping a shorter delay
    assert wait_for(lambda: ":WAVeform:SOURce CHANnel1" in server.lines())
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

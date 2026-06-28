"""Unit tests for RigolDHO800._read_full_waveform -- the batched :WAVeform:DATA?
loop that lets a large-memory-depth scope return its full record.

Hardware-free: a RigolDHO800 instance is built without connecting (object.__new__),
and ._write / ._read_block are stubbed to simulate a firmware that caps each
:WAVeform:DATA? at a configurable point count. Covers both BYTE (1 byte/point)
and WORD (2 bytes/point) -- :WAVeform:STARt/STOP and the window cap are counted in
points, while :WAVeform:DATA? returns bytes, so the loop must reconcile the two.

    pytest tests/test_rigol_chunked_read.py -v
"""

import pytest

from lab_scopes.rigol import RigolDHO800


class FakeScope(RigolDHO800):
    """RigolDHO800 with the SCPI transport replaced by an in-memory fake.

    Serves a ``total``-point record (``bytes_per_point`` bytes each), honouring
    whatever STARt/STOP the caller set, but never returning more than ``cap``
    *points* in one read (the simulated firmware per-transfer limit). ``cap=None``
    means "no cap". ``force_zero_after`` makes the Nth+ read return no data;
    ``dribble`` returns one point per read.
    """

    def __init__(self, total, bytes_per_point=1, cap=None,
                 force_zero_after=None, dribble=False):
        self.verbose = False
        self._total = total
        self._bpp = bytes_per_point
        self._cap = cap
        self._force_zero_after = force_zero_after
        self._dribble = dribble
        self._start = 1
        self._stop = total
        self._read_count = 0
        self.requested_windows = []  # (start, stop) point pairs seen by _write

    def _write(self, scpi):
        s = scpi.strip().upper()
        if s.startswith(':WAVEFORM:START'):
            self._start = int(s.rsplit(None, 1)[1])
        elif s.startswith(':WAVEFORM:STOP'):
            self._stop = int(s.rsplit(None, 1)[1])
            # STOP is always written right after STARt -> record the pair.
            self.requested_windows.append((self._start, self._stop))
        # ignore everything else

    def _read_block(self, scpi, timeout):
        self._read_count += 1
        if self._force_zero_after is not None and self._read_count >= self._force_zero_after:
            return b'', 0
        want = self._stop - self._start + 1  # points
        if want <= 0:
            return b'', 0
        if self._dribble:
            want = 1
        elif self._cap is not None:
            want = min(want, self._cap)
        # Distinct value per point offset so concatenation order is checkable
        # (mod 256 for BYTE, mod 65536 for WORD), little-endian to match the driver.
        payload = b''.join(
            ((self._start - 1 + i) % (256 ** self._bpp)).to_bytes(self._bpp, 'little')
            for i in range(want)
        )
        return payload, len(payload)


def _expected_record(n, bpp):
    return b''.join((i % (256 ** bpp)).to_bytes(bpp, 'little') for i in range(n))


@pytest.mark.parametrize('bpp', [1, 2], ids=['BYTE', 'WORD'])
def test_single_shot_no_cap(bpp):
    n = 100_000
    fs = FakeScope(total=n, bytes_per_point=bpp, cap=None)
    data = fs._read_full_waveform(n, bpp)
    assert len(data) == n * bpp
    assert fs._read_count == 1
    assert fs.requested_windows == [(1, n)]
    assert data == _expected_record(n, bpp)


@pytest.mark.parametrize('bpp', [1, 2], ids=['BYTE', 'WORD'])
def test_capped_tiles_full_record(bpp):
    n = 1_000_000
    cap = 250_000  # points per transfer
    fs = FakeScope(total=n, bytes_per_point=bpp, cap=cap)
    data = fs._read_full_waveform(n, bpp)
    assert len(data) == n * bpp
    # First request asks for the whole record; later ones use the observed cap.
    starts = [w[0] for w in fs.requested_windows]
    assert starts[0] == 1
    assert starts[1:] == [1 + cap, 1 + 2 * cap, 1 + 3 * cap]
    assert fs._read_count == 4
    assert data == _expected_record(n, bpp)  # reassembled in order


@pytest.mark.parametrize('bpp', [1, 2], ids=['BYTE', 'WORD'])
def test_capped_non_multiple(bpp):
    n = 1_000_001
    cap = 300_000
    fs = FakeScope(total=n, bytes_per_point=bpp, cap=cap)
    data = fs._read_full_waveform(n, bpp)
    assert len(data) == n * bpp
    expected_starts = [1, 1 + cap, 1 + 2 * cap, 1 + 3 * cap]  # last window = 1 point
    assert [w[0] for w in fs.requested_windows] == expected_starts


@pytest.mark.parametrize('bpp', [1, 2], ids=['BYTE', 'WORD'])
def test_first_chunk_cap_adopted(bpp):
    n = 500_000
    cap = 123_456
    fs = FakeScope(total=n, bytes_per_point=bpp, cap=cap)
    fs._read_full_waveform(n, bpp)
    widths = [stop - start + 1 for (start, stop) in fs.requested_windows]
    assert widths[0] == n              # first request: whole record (points)
    for w in widths[1:-1]:
        assert w == cap
    assert widths[-1] <= cap


@pytest.mark.parametrize('bpp', [1, 2], ids=['BYTE', 'WORD'])
def test_partial_record_after_persistent_empty(bpp):
    # Once a read yields no data and the bounded retries are exhausted, the loop
    # stops and returns the partial record (the caller raises on a short read).
    # With retries, the read_count is the 2 good chunks + (1 + _EMPTY_CHUNK_RETRIES)
    # empty attempts before giving up.
    n = 1_000_000
    cap = 250_000
    fs = FakeScope(total=n, bytes_per_point=bpp, cap=cap, force_zero_after=3)
    fs._EMPTY_CHUNK_BACKOFF = 0.0  # don't sleep in the test
    data = fs._read_full_waveform(n, bpp)
    assert len(data) == 2 * cap * bpp  # two full chunks landed before the empties
    assert fs._read_count == 3 + RigolDHO800._EMPTY_CHUNK_RETRIES


@pytest.mark.parametrize('bpp', [1, 2], ids=['BYTE', 'WORD'])
def test_dribbling_scope_still_completes(bpp):
    # A scope that returns one point per read still terminates: each positive
    # return advances `start`, so the loop runs n_total times and reassembles the
    # full record (slow, but correct -- not infinite).
    n = 500
    fs = FakeScope(total=n, bytes_per_point=bpp, dribble=True)
    data = fs._read_full_waveform(n, bpp)
    assert len(data) == n * bpp
    assert fs._read_count == n
    assert data == _expected_record(n, bpp)

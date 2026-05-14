import struct

import numpy as np

from lab_scopes.lecroy import LeCroyWavedesc, LeCroyScope, WAVEDESC_SIZE
from lab_scopes.lecroy.constants import WAVEDESC_FMT


TRACE_PREFIX = b"0" * 15


class SyntheticLeCroyScope(LeCroyScope):
    def __init__(self, trace_bytes):
        self._synthetic_trace_bytes = trace_bytes
        self.acquire_bytes_calls = []
        self.verbose = False
        self.valid_trace_names = ("C1",)

    def acquire_bytes(self, trace, seg=0):
        self.acquire_bytes_calls.append((trace, seg))
        wavedesc_start = len(TRACE_PREFIX)
        wavedesc_end = wavedesc_start + WAVEDESC_SIZE
        return self._synthetic_trace_bytes, self._synthetic_trace_bytes[wavedesc_start:wavedesc_end]


def _wavedesc_bytes(total_samples, comm_type=1, subarray_count=0, gain=0.25, offset=-1.5):
    wavedesc = LeCroyWavedesc()
    wavedesc.generate_test_data(NTimes=1)
    bytes_per_sample = 2 if comm_type == 1 else 1
    wd = wavedesc.wd._replace(
        comm_type=comm_type,
        wave_array_1=total_samples * bytes_per_sample,
        subarray_count=subarray_count,
        vertical_gain=gain,
        vertical_offset=offset,
    )
    return struct.pack(WAVEDESC_FMT, *list(wd))


def _trace_bytes(samples, comm_type=1, subarray_count=0, gain=0.25, offset=-1.5):
    dtype = "<i2" if comm_type == 1 else np.int8
    data = np.asarray(samples, dtype=dtype)
    return (
        TRACE_PREFIX
        + _wavedesc_bytes(data.size, comm_type=comm_type, subarray_count=subarray_count, gain=gain, offset=offset)
        + data.tobytes()
    )


def test_acquire_parses_word_data_as_int16_frombuffer():
    samples = np.array([-32768, -2, 0, 17, 32767], dtype="<i2")
    scope = SyntheticLeCroyScope(_trace_bytes(samples, comm_type=1))

    data, wavedesc_bytes = scope.acquire("C1", raw=True)

    assert len(wavedesc_bytes) == WAVEDESC_SIZE
    assert data.dtype == np.dtype("<i2")
    np.testing.assert_array_equal(data, samples)


def test_acquire_scales_word_data_when_raw_false():
    samples = np.array([-4, 0, 12], dtype="<i2")
    gain = 0.125
    offset = -2.0
    scope = SyntheticLeCroyScope(_trace_bytes(samples, comm_type=1, gain=gain, offset=offset))

    data, _wavedesc_bytes = scope.acquire("C1", raw=False)

    assert data.dtype == np.float64
    np.testing.assert_allclose(data, samples.astype(np.float64) * gain - offset)


def test_acquire_parses_byte_data_fallback():
    samples = np.array([-128, -1, 0, 12, 127], dtype=np.int8)
    scope = SyntheticLeCroyScope(_trace_bytes(samples, comm_type=0))

    data, _wavedesc_bytes = scope.acquire("C1", raw=True)

    assert data.dtype == np.int8
    np.testing.assert_array_equal(data, samples)


def test_acquire_sequence_data_reads_once_and_preserves_segments():
    segments = np.array(
        [
            [-4, -3, -2, -1],
            [10, 11, 12, 13],
            [100, 101, 102, 103],
        ],
        dtype="<i2",
    )
    gain = 0.5
    offset = 1.0
    trace_bytes = _trace_bytes(
        segments.reshape(-1),
        comm_type=1,
        subarray_count=segments.shape[0],
        gain=gain,
        offset=offset,
    )
    scope = SyntheticLeCroyScope(trace_bytes)

    segment_data, wavedesc_bytes = scope.acquire_sequence_data("C1")

    assert len(wavedesc_bytes) == WAVEDESC_SIZE
    assert scope.acquire_bytes_calls == [("C1", 0)]
    assert len(segment_data) == segments.shape[0]
    expected = segments.astype(np.float64) * gain - offset
    for actual, expected_segment in zip(segment_data, expected):
        np.testing.assert_allclose(actual, expected_segment)

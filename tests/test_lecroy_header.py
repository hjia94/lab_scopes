import numpy as np
import pytest

from lab_scopes.lecroy import LeCroyHeader, WAVEDESC_SIZE


def test_generated_lecroy_header_roundtrip():
    h = LeCroyHeader()
    header_bytes = h.generate_test_data(NTimes=128)
    decoded = LeCroyHeader(header_bytes)

    assert len(header_bytes) == WAVEDESC_SIZE
    assert decoded.num_samples == 128
    assert decoded.dt == pytest.approx(0.001)
    assert decoded.t0 == pytest.approx(0.002)
    assert np.isfinite(decoded.time_array).all()
    assert len(decoded.time_array) == 128

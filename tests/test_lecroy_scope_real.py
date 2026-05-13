import numpy as np
import pytest

from lab_scopes.lecroy import LeCroyScope


SCOPE_IP = None  # Set to your scope IP address, for example: "192.168.1.100"
TRACE = "C1"


@pytest.mark.skipif(not SCOPE_IP, reason="set SCOPE_IP in this test file to test a real LeCroy scope")
def test_real_lecroy_acquire_word_data():
    with LeCroyScope(SCOPE_IP, verbose=False, timeout=10.0) as scope:
        raw_data, header_bytes = scope.acquire(TRACE, raw=True)

    assert len(header_bytes) > 0
    assert raw_data.dtype == np.dtype("<i2")
    assert raw_data.size > 0

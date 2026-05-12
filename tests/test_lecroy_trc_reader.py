import os
from pathlib import Path

import numpy as np
import pytest

from lab_scopes.io.lecroy_files import (
    get_trigger_time,
    read_trc_data_no_header,
    read_trc_data_simplified,
)


def _trc_dir():
    return Path(os.environ.get("LAB_SCOPES_TRC_DIR", r"D:\data\raw data"))


def _sample_trc():
    preferred = _trc_dir() / "C1-interf-shot00000.trc"
    if preferred.exists():
        return preferred
    matches = sorted(_trc_dir().glob("*.trc"))
    if not matches:
        pytest.skip(f"no .trc files found in {_trc_dir()}")
    return matches[0]


def test_read_trc_data_simplified_from_raw_data_dir():
    path = _sample_trc()
    data, tarr, gain, offset = read_trc_data_simplified(path)

    assert data.shape == tarr.shape
    assert data.size > 0
    assert np.isfinite(data).all()
    assert np.isfinite(tarr).all()
    assert np.isfinite(gain)
    assert np.isfinite(offset)
    assert np.all(np.diff(tarr[: min(1000, len(tarr))]) > 0)


def test_read_trc_data_no_header_matches_simplified():
    path = _sample_trc()
    data, tarr, gain, offset = read_trc_data_simplified(path)
    data_no_header = read_trc_data_no_header(path, len(tarr), gain, offset)

    np.testing.assert_allclose(data_no_header, data)


def test_get_trigger_time_has_expected_fields():
    path = _sample_trc()
    trigger = get_trigger_time(path)

    assert set(trigger) == {"year", "month", "day", "hour", "minute", "second"}
    assert 1 <= trigger["month"] <= 12
    assert 1 <= trigger["day"] <= 31

import numpy as np
import pytest

from lab_scopes.lecroy import LeCroyHeader

h5py = pytest.importorskip("h5py")


def test_hdf5_scope_reader_generated_file(tmp_path):
    from lab_scopes.io.hdf5 import read_hdf5_all_scopes_channels, read_hdf5_scope_data

    header = LeCroyHeader()
    header_bytes = header.generate_test_data(NTimes=8)
    raw = np.arange(8, dtype=np.int16)
    path = tmp_path / "scope.h5"

    with h5py.File(path, "w") as f:
        scope = f.create_group("bdotscope")
        scope.create_dataset("time_array", data=np.arange(8) * 0.001 + 0.002)
        shot = scope.create_group("shot_1")
        shot.create_dataset("C1_data", data=raw)
        shot.create_dataset("C1_header", data=np.void(header_bytes))

    with h5py.File(path, "r") as f:
        data, dt, t0 = read_hdf5_scope_data(f, "bdotscope", "C1", 1)
        all_data = read_hdf5_all_scopes_channels(f, 1)

    np.testing.assert_allclose(data, raw * 0.1 - 0.2)
    assert dt == pytest.approx(0.001)
    assert t0 == pytest.approx(0.002)
    assert "bdotscope" in all_data

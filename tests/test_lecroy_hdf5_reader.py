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


def test_read_hdf5_scope_channel_shots(tmp_path):
    from lab_scopes.io.hdf5 import read_hdf5_scope_channel_shots, read_hdf5_scope_data

    header = LeCroyHeader()
    header_bytes = header.generate_test_data(NTimes=8)
    path = tmp_path / "scope.h5"

    # shots: 1 good, 2 good, 3 skipped, 4 missing entirely, 5 wrong length
    with h5py.File(path, "w") as f:
        scope = f.create_group("bdotscope")
        scope.create_dataset("time_array", data=np.arange(8) * 0.001 + 0.002)
        for s, raw in ((1, np.arange(8, dtype=np.int16)),
                       (2, np.arange(8, 16, dtype=np.int16))):
            shot = scope.create_group(f"shot_{s}")
            shot.create_dataset("C1_data", data=raw)
            shot.create_dataset("C1_header", data=np.void(header_bytes))
        skip = scope.create_group("shot_3")          # skipped
        skip.attrs["skipped"] = True
        short = scope.create_group("shot_5")          # wrong length
        short.create_dataset("C1_data", data=np.arange(4, dtype=np.int16))
        short.create_dataset("C1_header", data=np.void(header_bytes))

    with h5py.File(path, "r") as f:
        stack, dt, t0 = read_hdf5_scope_channel_shots(
            f, "bdotscope", "C1", [1, 2, 3, 4, 5], expected_len=8)
        single1, _, _ = read_hdf5_scope_data(f, "bdotscope", "C1", 1)
        single2, _, _ = read_hdf5_scope_data(f, "bdotscope", "C1", 2)

    assert stack.shape == (5, 8)
    # good shots match the per-shot reader exactly (float64, bit-for-bit)
    np.testing.assert_array_equal(stack[0], single1)
    np.testing.assert_array_equal(stack[1], single2)
    # skipped (3), missing (4), wrong-length (5) -> NaN rows
    assert np.all(np.isnan(stack[2]))
    assert np.all(np.isnan(stack[3]))
    assert np.all(np.isnan(stack[4]))
    assert dt == pytest.approx(0.001)
    assert t0 == pytest.approx(0.002)


def test_read_hdf5_scope_channel_shots_none_when_unreadable(tmp_path):
    from lab_scopes.io.hdf5 import read_hdf5_scope_channel_shots

    path = tmp_path / "scope.h5"
    with h5py.File(path, "w") as f:
        scope = f.create_group("bdotscope")
        skip = scope.create_group("shot_1")
        skip.attrs["skipped"] = True

    with h5py.File(path, "r") as f:
        stack, dt, t0 = read_hdf5_scope_channel_shots(f, "bdotscope", "C1", [1, 2])
    assert stack is None and dt is None and t0 is None

import h5py

from lab_scopes.io.hdf5 import read_hdf5_all_scopes_channels


path = "example_scope_data.hdf5"
with h5py.File(path, "r") as f:
    data = read_hdf5_all_scopes_channels(f, shot_number=1)
print(data.keys())

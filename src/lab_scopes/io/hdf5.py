"""HDF5 reader helpers for scope data archives."""

from .lecroy_files import (
    open_hdf5_readonly,
    read_hdf5_all_scopes_channels,
    read_hdf5_scope_data,
    read_hdf5_scope_tarr,
    read_scope_channel_descriptions,
)

__all__ = [
    "open_hdf5_readonly",
    "read_hdf5_all_scopes_channels",
    "read_hdf5_scope_data",
    "read_hdf5_scope_tarr",
    "read_scope_channel_descriptions",
]

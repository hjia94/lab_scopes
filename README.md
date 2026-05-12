# lab_scopes

Reusable oscilloscope drivers and offline readers for LAPD/BaPSF-style scope data.

The package is intentionally usable without PyVISA. LeCroy live communication is
planned around native VICP-over-TCP, Rigol DHO800/DHO900 communication uses plain
TCP/SCPI, and LeCroy `.trc` readers work offline.

## Install

```powershell
pip install -e .
```

Optional HDF5 helpers:

```powershell
pip install -e ".[hdf5]"
```

## Imports

New code:

```python
from lab_scopes.lecroy import LeCroyScope, LeCroyHeader
from lab_scopes.rigol import RigolDHO800, RigolScope
from lab_scopes.io.lecroy_files import read_trc_data_simplified
```

Legacy shims are also shipped for gradual migration:

```python
from LeCroy_Scope import LeCroy_Scope
from LeCroy_Scope_Header import LeCroy_Scope_Header
from read_scope_data import read_trc_data_simplified
from rigol_scope import RigolScope
from rigol_dho800 import RigolDHO800
```

## Tests

The test suite does not connect to real oscilloscopes. LeCroy `.trc` tests use
`D:\data\raw data` by default, or `LAB_SCOPES_TRC_DIR` if set.

# lab_scopes

Reusable oscilloscope drivers and offline readers for BaPSF style scope data.

LeCroy communication uses native VICP/TCP;
Rigol DHO800/DHO900 communication uses plain TCP/SCPI.

LeCroy `.trc` readers work offline.

## Install

```terminal
pip install "git+https://github.com/hjia94/lab_scopes.git"
```

Optional HDF5 helpers:

```terminal
pip install "lab-scopes[hdf5] @ git+https://github.com/hjia94/lab_scopes.git"
```

For development: pip install -e .

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
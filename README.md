# lab_scopes

Reusable oscilloscope drivers and offline readers for scope data.

- **LeCroy** — native VICP/TCP driver for (`LeCroyScope`)
- **Rigol DHO800/DHO900** — plain TCP/SCPI driver for (`RigolDHO800`)


## What's new in 0.3.2

- **Fix: C5–C8 silently dropped on 8-channel LeCroy scopes (data loss).
- Now both 4 and 8-channel scopes are supported.


## What's new in 0.3.0

- **8-channel LeCroy support.**
- **Master/slave synchronized acquisition.**
New arming primitives for using multiple scopes together on acquisition. The main purpose is to make sure all scopes are triggered for the same shot.

**Known limitation:** sequence/segment acquisition is still unavailable

## What's new in 0.2.0

- Faster LeCroy waveform acquisition (optimized VICP transfer path, single-fetch raw↔scaled cross-check).
- Hardened SCPI/VBS response handling.
- Renamed internals for clarity: `header` → `wavedesc`, `HEADER_SIZE` → `VICP_FRAME_HEADER_SIZE`.
- Add hardware test suite (`tests/test_lecroy_scope_real.py`)

**Known limitation:** sequence/segment acquisition is currently disabled and not available in this release. Use single-shot acquisition; segment mode will return in a later version.

## Install

Requires **Python 3.11+** (developed and tested on 3.14).

```terminal
pip install "git+https://github.com/hjia94/lab_scopes.git"
```

Optional HDF5 helpers:

```terminal
pip install "lab-scopes[hdf5] @ git+https://github.com/hjia94/lab_scopes.git"
```

For development: pip install -e .

## Imports

```python
from lab_scopes.lecroy import LeCroyScope, LeCroyWavedesc
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

## Rigol DHO800/DHO900

`RigolDHO800` connects over plain TCP/SCPI and reads the full acquisition record
(not just the on-screen window). It batches `:WAVeform:DATA?` transfers to work
around the firmware's per-transfer cap and applies the calibration formula from
the programming guide.

```python
from lab_scopes.rigol import RigolDHO800

scope = RigolDHO800("192.168.1.50")
scope.single()                       # arm a single capture
scope.wait_until_stopped()           # block until the acquisition completes
for ch in scope.displayed_channels():
    wf = scope.read_channel(ch)      # returns a Waveform (raw + calibration)
    print(ch, wf.points)             # voltage samples vs time
scope.screen_png("capture.png")      # save a screenshot
scope.close()
```

Key methods: `run` / `stop` / `single`, `set_sweep`, `trigger_status`,
`wait_until_stopped`, `displayed_channels`, `memory_depth`, `sample_rate`,
`vertical_scale` / `vertical_offset`, `timebase_scale` / `timebase_offset`,
`read_channel`, and `screen_png`.

## Tests

The test files import the installed `lab_scopes` package, so you do **not**
need to clone the repo. Install the package plus `pytest`, then download
the test file you want to run.

```terminal
pip install "git+https://github.com/hjia94/lab_scopes.git" pytest
```

### LeCroy scope

The file [tests/test_lecroy_scope_real.py](tests/test_lecroy_scope_real.py)
exercises ~20 areas of the `LeCroyScope` driver against a live instrument:
connection and `*IDN?`, channel-count detection, channel/trace validation,
displayed-channel discovery, `max_samples`, vertical scale, averaging, raw and
scaled acquisition with a single-fetch raw↔scaled cross-check, header parsing,
`time_array`, sequence mode (auto-skipped if not active), and status messages.
The master/slave arming and sweep-counter completion primitives are covered by
the offline [tests/test_lecroy_arm_sync.py](tests/test_lecroy_arm_sync.py)
suite (no hardware required).

The suite has two mutually exclusive modes selected by the
`MUTATING` flag:

- `MUTATING = False` (default) runs the read-only and acquisition tests
  above plus the end-of-session report.
- `MUTATING = True` runs **only** tests that will alter scope: trigger-mode
  cycling, `*CAL?` self-calibration (~15 s), and the vertical-scale and
  averaging-count round-trips.

1. Download the test file into any working directory, along with
   [tests/conftest.py](tests/conftest.py) (same directory) — it registers the
   `mutating` marker and implements the `MUTATING = True` filtering. Without
   it, `MUTATING = True` runs *all* tests instead of only the state-mutating
   ones.

2. Open `test_lecroy_scope_real.py` and edit the constants at the top:

   ```python
   SCOPE_IP  = "192.168.1.100"  # leave None to keep every test skipped
   MUTATING  = False            # True runs ONLY the state-mutating tests
   SHOW_PLOT = False            # True plots displayed traces at end of run
   ```
   - `SHOW_PLOT = True` → after the textual report, opens a matplotlib
     figure with one subplot per displayed trace (V vs s, axis units
     auto-scaled). Requires `MUTATING = False`.

3. Run with `-s` so the end-of-session report prints live:

   ```terminal
   pytest test_lecroy_scope_real.py -v -s
   ```

   The report lists per-trace metadata (samples, dt, sampling rate,
   vertical gain/offset, V/div, coupling, record type, timebase,
   sweeps_per_acq, segments), transfer timings (bytes / seconds / MB/s
   from a warm post-warmup `acquire_bytes`), a PASS/SKIP line for every
   test with a one-line fact, and any non-fatal warnings (empty
   `valid_trace_names`, only one displayed trace, etc.).

Trace selection is automatic: the suite calls `displayed_traces()` /
`displayed_channels()` and uses the first one available. Per-trace tests
skip individually if nothing is on screen.

### Offline suite (no hardware)

Most of the suite needs no instrument. If you clone the repo, run the full
offline set with:

```terminal
pip install -e ".[dev]"
pytest -v
```

The offline tests cover:

- `test_lecroy_scope_acquire.py` — synthetic acquire path (raw↔scaled).
- `test_lecroy_channel_detect.py` — 4-vs-8 channel detection and its
  connection-drop / unavailable-query robustness.
- `test_lecroy_arm_sync.py` — master/slave arming and sweep-counter completion
  primitives.
- `test_lecroy_header.py`, `test_lecroy_trc_reader.py`,
  `test_lecroy_hdf5_reader.py` — WAVEDESC parsing and the `.trc` / HDF5 readers.
- `test_lecroy_vicp_framing.py` — VICP frame handling.
- `test_legacy_imports.py`, `test_rigol_imports.py`,
  `test_imports_no_pyvisa.py` — legacy shims, Rigol exports, and that the
  package imports without `pyvisa` installed.

The two hardware-only files (`test_lecroy_scope_real.py`, above) stay skipped
until you point them at a scope.
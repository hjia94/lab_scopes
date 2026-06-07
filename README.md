# lab_scopes

Reusable oscilloscope drivers and offline readers for BaPSF style scope data.

LeCroy communication uses native VICP/TCP;
Rigol DHO800/DHO900 communication uses plain TCP/SCPI.

LeCroy `.trc` readers work offline.

## What's new in 0.3.0

- **8-channel LeCroy support.** The driver detects the analog input count (4 or
  8) at connect time — a zero-round-trip `*IDN?` model match, falling back to a
  single `C5` probe for unrecognized models — and bounds channel/trace
  validation and displayed-channel scans to the channels that actually exist, so
  it never stalls probing C5–C8 on a 4-channel scope. `channel_names` /
  `n_channels` expose the detected layout.
- **Master/slave synchronized acquisition.** New arming primitives let a caller
  arm every slave first, confirm each is genuinely trigger-ready via the INR
  status register (`arm_single_and_confirm` / `wait_for_trigger_ready`), then arm
  the master last exactly once (`arm_master_single`) — so the master's
  trigger-out can't fire before a slave is listening. Per-shot completion is
  verified by the WAVEDESC sweep counter (`wait_for_stop_then_complete`), which
  distinguishes a fresh capture from a leftover STOP.
- The master arm no longer emits a spurious per-shot warning when it arms and
  fires before the trigger-mode read-back (a healthy `STOP` against a fast
  free-running timer); it warns only on a genuinely unexpected mode.

**Known limitation:** sequence/segment acquisition is still disabled (see
below); use single-shot acquisition.

## What's new in 0.2.0

- Faster LeCroy waveform acquisition (optimized VICP transfer path, single-fetch raw↔scaled cross-check).
- Hardened SCPI/VBS response handling: drained query responses for trace-name probe and `*CAL?`, graceful handling of `OFF` VBS replies and empty `:WAVEFORM?` buffers, corrected socket-timeout behavior.
- Renamed internals for clarity: `header` → `wavedesc`, `HEADER_SIZE` → `VICP_FRAME_HEADER_SIZE`.
- Expanded real-hardware test suite (`tests/test_lecroy_scope_real.py`) with end-of-session report, optional waveform plotting, and a `MUTATING` flag for state-altering tests.

**Known limitation:** sequence/segment acquisition is currently disabled and not available in this release. Use single-shot acquisition; segment mode will return in a later version.

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

1. Download the test file into any working directory.

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

If you do clone the repo, the offline synthetic suite runs with:

```terminal
pip install -e ".[dev]"
pytest tests/test_lecroy_scope_acquire.py -v
```
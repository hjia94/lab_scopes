# lab_scopes

**Reusable oscilloscope drivers and offline readers for lab data.**

Native, dependency-light drivers for LeCroy and Rigol oscilloscopes, plus
offline readers for LeCroy `.trc` and HDF5 files. No VISA runtime required —
the drivers speak the instruments' wire protocols directly over TCP.

| Instrument | Class | Transport |
| --- | --- | --- |
| LeCroy (WaveRunner / HDO / etc.) | `LeCroyScope` | native VICP / TCP |
| Rigol DHO800 / DHO900 | `RigolDHO800` | plain TCP / SCPI |

---

## Install

Requires **Python 3.11+** (developed and tested on 3.14). The only hard
dependency is `numpy`.

<!-- Not yet published to PyPI; restore once released.
```bash
pip install lab-scopes
```
-->

```bash
pip install "git+https://github.com/hjia94/lab_scopes.git"
```

Optional extras:

```bash
pip install "lab-scopes[hdf5] @ git+https://github.com/hjia94/lab_scopes.git"   # HDF5 file readers (h5py)
pip install "lab-scopes[plot] @ git+https://github.com/hjia94/lab_scopes.git"   # matplotlib helpers
pip install "lab-scopes[dev] @ git+https://github.com/hjia94/lab_scopes.git"    # pytest + h5py for the test suite
```

---

## Quick start

### LeCroy

```python
from lab_scopes.lecroy import LeCroyScope

with LeCroyScope("192.168.1.100") as scope:
    for trace in scope.displayed_traces():
        data, wavedesc = scope.acquire(trace)   # scaled volts + raw WAVEDESC
        time = scope.time_array(trace)
        print(trace, len(data))
```

### Rigol DHO800 / DHO900

`RigolDHO800` reads the **full acquisition record** (not just the on-screen
window). It batches `:WAVeform:DATA?` transfers around the firmware's
per-transfer cap and applies the calibration formula from the programming
guide.

```python
from lab_scopes.rigol import RigolDHO800

scope = RigolDHO800("192.168.1.50")
scope.single()                       # arm a single capture
scope.wait_until_stopped()           # block until acquisition completes
for ch in scope.displayed_channels():
    wf = scope.read_channel(ch)      # Waveform: raw samples + calibration
    print(ch, wf.points)             # voltage samples vs time
scope.screen_png("capture.png")      # save a screenshot
scope.close()
```

Key methods: `run` / `stop` / `single`, `set_sweep`, `trigger_status`,
`wait_until_stopped`, `displayed_channels`, `memory_depth`, `sample_rate`,
`vertical_scale` / `vertical_offset`, `timebase_scale` / `timebase_offset`,
`read_channel`, and `screen_png`.

**Time limits and errors.** Every wait has its own ceiling (15 s per text query,
a byte-scaled ceiling per waveform chunk). Pass a `Deadline` to cap a whole
operation:

```python
from lab_scopes.transports import Deadline

with RigolDHO800(ip, timeout=1.0, deadline=Deadline(2.5)) as scope:  # connect + reads
    scope.stop()
    wf = scope.read_channel(1)
```

`scope.deadline` can be replaced between operations. A settle delay or retry
backoff is never shortened to fit; the operation fails instead.

Failures raise `lab_scopes.errors` types: `ScopeTimeoutError`,
`ScopeConnectionError`, `ScopeProtocolError`, and `RigolScopeError` for
device-state problems. All are `ScopeError`, a `RuntimeError`. After a
transport failure the connection is closed and later calls raise
`ScopeConnectionError`: port 5555 has no device clear, so a late reply would
otherwise answer the next query. Open a new `RigolDHO800` to reconnect.

### Offline file readers

```python
from lab_scopes.io.lecroy_files import read_trc_data_simplified

volts, time, gain, offset = read_trc_data_simplified("capture.trc")
```

---

## What's new in 0.5.0

- **Rigol time limits without signals.** An optional `Deadline` bounds a whole
  Rigol operation (connect, queries, waveform reads, settle delays), on
  Windows and Linux. Commands sent and replies decoded are unchanged.
- **Rigol error handling.** Timeouts, lost connections and malformed replies
  raise distinct `ScopeError` types instead of being retried silently or
  returned as partial data. A timed-out query no longer returns its default
  value, and a failed connection is closed rather than reused.

## What's new in 0.4.0

- **Sequence / segment acquisition is back.** LeCroy multi-segment captures
  are supported via `acquire_sequence_data()` and per-segment trigger
  timestamps via `get_sequence_trigger_times()`.
- **Rigol 12-bit full-memory acquisition.** DHO800/DHO900 reads the complete
  deep-memory record with the correct 12-bit calibration, not just the screen
  window.
- **Master/slave synchronized acquisition** for triggering multiple LeCroy
  scopes on the same shot (`arm_master_single`, `wait_for_stop_then_complete`).
- **8-channel LeCroy support** — C1–C8 are all captured (earlier releases
  silently dropped C5–C8 on 8-channel scopes).

<details>
<summary>Earlier releases</summary>

- **0.3.x** — 8-channel LeCroy support, master/slave arming primitives, and a
  fix for silently dropped C5–C8 channels.
- **0.2.0** — faster LeCroy waveform acquisition (optimized VICP path),
  hardened SCPI/VBS response handling, and the first real-hardware test suite.

</details>

---

## Imports

Modern package layout:

```python
from lab_scopes.lecroy import LeCroyScope, LeCroyWavedesc
from lab_scopes.rigol import RigolDHO800, RigolScope
from lab_scopes.io.lecroy_files import read_trc_data_simplified
```

Legacy flat-module shims are still shipped for gradual migration:

```python
from LeCroy_Scope import LeCroy_Scope
from LeCroy_Scope_Header import LeCroy_Scope_Header
from read_scope_data import read_trc_data_simplified
from rigol_scope import RigolScope
from rigol_dho800 import RigolDHO800
```

---

## Tests

The test files import the installed `lab_scopes` package, so you do **not**
need to clone the repo to run the hardware suites — install the package plus
`pytest`, then download the test file you want:

```bash
pip install "git+https://github.com/hjia94/lab_scopes.git" pytest
```

### LeCroy hardware suite

[tests/test_lecroy_scope_real.py](tests/test_lecroy_scope_real.py) exercises
~20 areas of the `LeCroyScope` driver against a live instrument: connection and
`*IDN?`, channel-count detection, channel/trace validation, displayed-channel
discovery, `max_samples`, vertical scale, averaging, raw and scaled acquisition
with a single-fetch raw↔scaled cross-check, header parsing, `time_array`,
sequence mode (auto-skipped if not active), and status messages. The
master/slave arming and sweep-counter completion primitives are covered offline
by [tests/test_lecroy_arm_sync.py](tests/test_lecroy_arm_sync.py) (no hardware
required).

The suite has two mutually exclusive modes selected by the `MUTATING` flag:

- `MUTATING = False` (default) runs the read-only and acquisition tests plus
  the end-of-session report.
- `MUTATING = True` runs **only** state-mutating tests: trigger-mode cycling,
  `*CAL?` self-calibration (~15 s), and the vertical-scale and averaging-count
  round-trips.

1. Download the test file into any working directory, along with
   [tests/conftest.py](tests/conftest.py) (same directory) — it registers the
   `mutating` marker and implements `MUTATING = True` filtering. Without it,
   `MUTATING = True` runs *all* tests instead of only the state-mutating ones.

2. Edit the constants at the top of `test_lecroy_scope_real.py`:

   ```python
   SCOPE_IP  = "192.168.1.100"  # leave None to keep every test skipped
   MUTATING  = False            # True runs ONLY the state-mutating tests
   SHOW_PLOT = False            # True plots displayed traces at end of run
   ```

   With `SHOW_PLOT = True` (and `MUTATING = False`), the run ends with a
   matplotlib figure — one subplot per displayed trace, axis units auto-scaled.

3. Run with `-s` so the end-of-session report prints live:

   ```bash
   pytest test_lecroy_scope_real.py -v -s
   ```

   The report lists per-trace metadata (samples, dt, sampling rate, vertical
   gain/offset, V/div, coupling, record type, timebase, sweeps_per_acq,
   segments), warm transfer timings (bytes / seconds / MB/s), a PASS/SKIP line
   for every test with a one-line fact, and any non-fatal warnings.

Trace selection is automatic: the suite uses the first available result of
`displayed_traces()` / `displayed_channels()`, and per-trace tests skip
individually if nothing is on screen.

### Offline suite (no hardware)

Most of the suite needs no instrument. From a clone:

```bash
pip install -e ".[dev]"
pytest -v
```

The offline tests cover:

- `test_lecroy_scope_acquire.py` — synthetic acquire path (raw↔scaled).
- `test_lecroy_channel_detect.py` — 4-vs-8 channel detection and its
  connection-drop / unavailable-query robustness.
- `test_lecroy_arm_sync.py` — master/slave arming and sweep-counter completion.
- `test_lecroy_header.py`, `test_lecroy_trc_reader.py`,
  `test_lecroy_hdf5_reader.py` — WAVEDESC parsing and the `.trc` / HDF5 readers.
- `test_lecroy_vicp_framing.py` — VICP frame handling.
- `test_rigol_chunked_read.py` — batched Rigol waveform reads.
- `test_rigol_deadline.py` — Rigol deadlines and error handling against a
  loopback fake scope, including the exact command bytes sent.
- `test_legacy_imports.py`, `test_rigol_imports.py`,
  `test_imports_no_pyvisa.py` — legacy shims, Rigol exports, and that the
  package imports without `pyvisa` installed.

---

## License

MIT — see [LICENSE](LICENSE).

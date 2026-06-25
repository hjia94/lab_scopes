# Rigol DHO800/DHO900 Driver — Analysis & Firmware History

> Compacted working notes from a code-analysis session. Use this as the starting
> brief for a dedicated Rigol workspace.

## Where the code lives

- **Current driver:** `lab_scopes` → `src/lab_scopes/rigol/dho800.py` (class `RigolDHO800`)
- **Transport:** `src/lab_scopes/transports/rigol_functions.py` (`command`, TMC helpers, `_raw_socket_recv`)
- **Legacy shim:** `src/rigol_dho800.py` (just `from lab_scopes.rigol.dho800 import *`)
- **Origin repo (full git history):** `Bapsf/bapsf_dimagnetic` — lab_scopes and LAPD_DAQ
  only carry the squashed result, so all the *why* is in bapsf_dimagnetic's log.

Validated against: **DHO800 Series Programming Guide (EN)**,
`C:\Users\hjia9\Documents\Papers\manual\DHO800-Series_programmingguide_EN.pdf`.
Hardware the history was validated on: **DHO804, firmware 00.01.03**.

**12-bit / WORD acquisition validated on firmware 00.01.05** (2026-06-19, bench test).
`read_channel` now defaults to `:WAVeform:FORMat WORD` (2 bytes/point, full 12-bit
ADC code parsed as little-endian `<u2`); the batched read counts STARt/STOP in points
while DATA? returns bytes. Endianness, 2-bytes/point, 12-bit range, and full-depth
reassembly confirmed on hardware. BYTE remains callable for cross-checks.

---

## Guide cross-check — all SCPI verified correct

Every command the driver sends exists in the guide with matching parameters/semantics:
`:WAVeform:SOURce/MODE/FORMat/DATA?/STARt/STOP/XINCrement?/XORigin?/XREFerence?/`
`YINCrement?/YORigin?/YREFerence?`, `:ACQuire:MDEPth?/SRATe?`,
`:TRIGger:STATus?` (returns TD/WAIT/RUN/AUTO/STOP — matches `wait_until_stopped`),
`:TRIGger:SWEep` (AUTO/NORMal/SINGle), `:DISPlay:DATA?`, `:RUN/:STOP/:SINGle`.

Key guide facts:
- §3.28.2 — **MAXimum** mode: in **Stop** state reads the full captured internal-memory
  record; in Run state reads only the on-screen window. **RAW** also reads internal
  memory but only in Stop state.
- §3.28.12/.13 — STARt/STOP range in **MAX + Stop** is `1 .. current max memory depth`
  (in RAW it's also `1 .. mdepth`; in NORMal it's `1 .. 1000`).
- §3.28.9/.10 — In MAX/Stop, `YINCrement`/`YORigin` are "related to the internal-waveform
  VerticalScale" (no closed form) → must be read from the scope.
- §3.3.2 — `:ACQuire:MDEPth?` query always returns scientific notation (a number) even in
  Auto mode. Authoritative sample count.
- §3.9.7 — `:DISPlay:DATA?[<type>]` where `<type>` ∈ {BMP|PNG|JPG}, **default BMP**.

---

## THE KEY DECISION: why `MODE MAXimum`, not the guide's `MODE RAW`

The driver went RAW → MAX. The guide *documents* RAW for internal-memory reads, but RAW
broke on DHO804 fw 00.01.03 in ways MAX/Stop avoids. **Do not switch back to RAW** without
re-validating on hardware — you'll reintroduce the bugs below.

### bapsf_dimagnetic commit timeline

| Date | Commit | Mode | Finding |
|------|--------|------|---------|
| 2026-04-23 | `c16fa70` | → RAW | First RAW + chunked reads for full memory depth. |
| 2026-05-07 | `378cc59` | RAW | "Half-real/half-junk" traces. Cause: trusting preamble `<points>`; stale `STOP=250000` in NVRAM → junk past real MDEPth. Fix: trust `:ACQuire:MDEPth?`. |
| 2026-05-07 | `19dff28` | RAW | **Per-chunk `:WAV:STARt/STOP` writes silently rejected (-200 errors); scope re-dumps the same 250000 bytes every chunk.** Chunk loop silently concatenated duplicates on MDEPth > 250k. |
| 2026-05-07 | `2a2a5cc` | RAW | Firmware exposes `:WAVeform:POINts` as a **separate, session-persistent** setting; `:WAV:STOP` alone doesn't update it, and the preamble reads `<points>` from it. |
| 2026-05-11 | `2bf5e61` | RAW | **`:WAVeform:STARt stuck at 750001; cannot align record to t=0`.** With large STOP, firmware clamps `STARt` to `STOP − window_max + 1` and refuses `STARt 1`. Needed a brittle "collapse STOP / re-assert STARt" dance. |
| 2026-05-11 | **`74a516b`** | → **MAX** | **The switch.** MAX/Stop returns the full record without RAW's STARt/STOP-window negotiation, **and `:WAVeform:PREamble?` read wrong values on this firmware** → calibration moved to individual `:WAVeform:X*?/Y*?` queries. |
| 2026-05-11 | `6546c74` | MAX | Calibration read directly from scope, preamble dropped. |
| 2026-05-11 | `a6c2dd5` | MAX | Clean forward-batching `_read_full_waveform` (learns per-transfer cap from first chunk). This is the current code. |
| 2026-05-12 | `c766479` | MAX | `wait_until_stopped()` blocking poll replaces one-shot stop check (race after `:SINGle`). |

### Why MAX wins on this firmware
- **RAW** clamps `:WAVeform:STARt` away from 1 when STOP is large → can't align to t=0
  without fragile window-collapsing; **and** its `:WAVeform:POINts`/preamble state is
  persisted and unreliable.
- **MAX/Stop** accepts `STARt 1 .. STOP MDEPth` directly → the simple forward-batching loop
  works. Calibration via per-parameter queries because the lumped preamble was wrong.

**Conclusion:** current `MODE MAXimum` is a deliberate, hardware-validated choice, not a
guide-deviation mistake. (An earlier code-review flag claiming it should be RAW was withdrawn.)
→ Worth adding an in-code comment near `read_channel` recording this, so it stops getting
re-flagged. (Doc-only change; not yet applied.)

---

## Open issues found in current code (not yet fixed)

Severity, location, and rationale. Nothing below has been changed.

### 🔴 High
1. **`screen_png` sends bare `:DISPlay:DATA?`** ([dho800.py:505]) but the guide default is
   **BMP**, while the code then requires `\x89PNG` and raises otherwise. Should send
   `:DISPlay:DATA? PNG`. *(User note: screen dump "seems to work so far" — deprioritized,
   may be firmware-default-PNG or untested. Verify on hardware before changing.)*

### 🟡 Medium
3. **Binary-mode auto-detect by substring** in `command()` ([rigol_functions.py:44-47]) —
   only `:WAVEFORM:DATA?` / `:DISPLAY:DATA?` trigger binary mode. Any other block-returning
   query (e.g. `:WAVeform:PREamble?`) would be read in text mode and truncate at first `\n`.
   Current callers are safe (`_read_block` forces `binary_data=True`), but the coupling is
   fragile. Suggest: rely solely on the explicit `binary_data` flag.
4. **`_read_full_waveform` aborts on a single zero-byte chunk** ([dho800.py:357-358]) →
   `read_channel` raises "incomplete waveform". A transient empty `:WAV:DATA?` (scope busy)
   is treated as terminal; no retry.

### 🟢 Low
5. Bare `except Exception: sleep(0.01)` in the recv loop ([rigol_functions.py:89-90]) masks
   socket failures as timeouts.
6. `command()` returns the string `"command error"` on write failure instead of raising
   ([rigol_functions.py:144-145]) — error-by-magic-string.
7. `fmt='WORD'` rejected at runtime though `Waveform.raw` docstring advertises uint16 and the
   guide fully specifies WORD (2 bytes/pt, §3.28.3). Feature gap.
8. `channel_name` ([dho800.py:218-219]): `s.replace('MATH','MATH')` is a no-op; MATH names
   bypass range validation (`MATH9` passes). Cosmetic + unreachable-on-this-driver path.

### Improvements (nice-to-have)
- After setting STARt/STOP, optionally read back `:WAVeform:STARt?/STOP?` once near the
  memory-depth boundary to catch firmware window clamping.
- `get_memory_depth` fallback `12000` ([rigol_functions.py:193-200]) is a silent magic
  default — name it + log when used.
- telnetlib is removed in Python 3.13; the driver imports a patched `Telnet`. Binary reads
  already bypass it via `_raw_socket_recv` — consider a thin raw-socket reader to drop the
  dependency.
- `displayed_channels` ([dho800.py:271-291]) duplicates the per-group `:DISPlay?` scan —
  extract a helper.

---

## Suggested first steps in the new Rigol workspace

1. Bring up the driver against the real DHO804 and confirm `read_channel` returns a full
   MDEPth record (the original goal that MAX-mode solved).
2. Decide #1 (`screen_png` PNG arg) **on hardware** — confirm what bare `:DISPlay:DATA?`
   actually returns on your firmware before changing it.
3. Add the explanatory MAX-vs-RAW comment in `read_channel` so the decision survives.
4. Consider WORD-format support (#7) if 8-bit vertical resolution is limiting.

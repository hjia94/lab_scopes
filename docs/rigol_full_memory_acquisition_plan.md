# Plan — Rigol DHO800 full-memory-depth acquisition

> **Confirmed contract (2026-06-19, with the user):**
> - The user sets **Mem Depth on the scope's front panel** (not via remote control).
>   The front-panel timebase + Mem Depth together fix the sample rate. Software
>   **reads everything back and sets nothing** — not the rate, not the depth.
> - Software returns the **full Mem Depth record** — every sample the scope
>   captured (`:ACQuire:MDEPth?` points). This is inherently **≥ the on-screen
>   window**; the screen is just a sub-slice of it.
> - **No dropped samples, no padding, all samples accurate.** Accuracy is the top
>   priority — a short/partial read must *fail*, never return a quietly-trimmed array.
> - **12-bit vertical resolution is REQUIRED** (user, 2026-06-19). The DHO800 is a
>   12-bit ADC; the current BYTE (8-bit) read throws away 4 bits / 16× of vertical
>   resolution. Acquisition MUST use **WORD format** (2 bytes/point, full 12-bit code
>   in a 16-bit word, §3.28.3). BYTE is no longer acceptable for data acquisition.
>   *This promotes WORD support from "deferred" to a blocking deliverable (§3.8, §6).*
>
> **Validation target firmware: 00.01.05** (`00.01.05.00.02`, 2026/05/19 — current
> Rigol release). The user upgrades **one** scope from 00.01.02 → 00.01.05 and runs
> the §5 bench suite on it. Rationale: the firmware adds deeper Mem Depth options the
> user wants to validate (changelog `00.01.03.00.04`: *"Added 5M memory depth option
> for DHO800 series"*), and 00.01.02 cannot reach them. The prior validated firmware
> was **00.01.03** — see §8 for the changelog evaluation and the probe-ratio caution
> that makes 00.01.05 a *fresh* validation, not a carry-over.
>
> **Deep-record test ceiling: 25M points** (25,000,000 samples per channel) — the
> real working ceiling for the DHO800 fleet. All deep-read sizing, timeout, and
> verification work targets **25M** as the maximum. (Note: the changelog's 5M option
> is a stepping stone; the panel exposes larger depths up to 25M on these models.)
>
> **Note on the "acquired ≠ screen" surprise:** this is *expected*, not a bug. In
> MAX/Stop mode the scope returns the full internal record behind the screen, which
> is larger than the visible window. The plan does **not** trim to the screen; it
> keeps the full record and instead makes the relationship *documented and
> verifiable* so bench-testing stops being confusing (see §5).
>
> Grounded in the git history of the Rigol driver across `bapsf_dimagnetic`,
> `lab_scopes`, and `bapsf_interferometer`, and in the DHO800 Programming Guide.
> Companion to [`rigol_dho800_investigation.md`](rigol_dho800_investigation.md).

---

## 1. What the git history already settled (do not relitigate)

The acquisition strategy is the product of a documented, hardware-validated
RAW→MAX migration on **DHO804, firmware 00.01.03**. The full-memory-depth problem
was *solved*; the timeline is the proof of why each piece exists:

| Date | Commit | What it established |
|------|--------|---------------------|
| 04-23 | `c16fa70` | First attempt at full depth: RAW mode + chunked reads. |
| 05-07 | `378cc59` | **`:ACQuire:MDEPth?` is the only authoritative sample count.** Preamble `<points>` lies (stale NVRAM `STOP`). |
| 05-07 | `2a2a5cc`/`19dff28` | RAW's `:WAVeform:POINts` is a separate, session-persistent setting; `:WAV:STOP` alone doesn't update it. RAW chunking silently concatenated duplicate 250k dumps. |
| 05-11 | `2bf5e61` | RAW clamps `:WAVeform:STARt` to `STOP − window + 1` when STOP is large → cannot align record to t=0. |
| 05-11 | **`74a516b`** | **The switch to `MODE MAXimum`.** MAX/Stop returns the full internal record with `STARt 1 .. STOP MDEPth` accepted directly; preamble read wrong values → calibration moved to per-parameter `:WAVeform:X*?/Y*?` queries. |
| 05-11 | `6546c74`/`a6c2dd5` | Calibration read straight from scope; clean forward-batching `_read_full_waveform` that learns the per-transfer cap from the first chunk. **This is the current code.** |
| 05-12 | `c766479` | `wait_until_stopped()` blocking poll (race after `:SINGle`). |

**Conclusion the history forces:** `MODE MAXimum` + Stop-state + per-parameter
calibration + forward batching is the *correct, validated* full-depth strategy on
this firmware. **RAW is a regression** — re-adopting it reintroduces the STARt-clamp
and POINts-persistence bugs above. The plan below *hardens and verifies* the
existing MAX strategy; it does not redesign it.

---

## 2. The accuracy contract (what "correct full-depth acquisition" means)

A single `read_channel(ch)`, with the scope STOPped after a real trigger, must
return a `Waveform` where:

1. **Length == `:ACQuire:MDEPth?`** — the full internal record (the Mem Depth the
   user dialed in on the panel), **not** the on-screen subset. MAX/Stop (§3.28.2)
   returns the record behind the screen window, so this is always ≥ screen length.
2. **No duplicated, dropped, or zero-padded samples** — batches are strictly
   consecutive (`start += got`) and cover `1..MDEPth` with no gap/overlap.
3. **Full 12-bit resolution via WORD format** — read `:WAVeform:FORMat WORD`
   (2 bytes/point, §3.28.3); parse as little-endian `uint16` carrying the 12-bit code.
   BYTE (8-bit) is not acceptable — it discards 4 bits of the 12-bit ADC.
4. **0xFF / 0xFFFF byte values survive** — binary read bypasses telnetlib IAC
   (`_raw_socket_recv`); with WORD, *either* byte of a 16-bit word can be 0xFF, so the
   IAC-bypass matters even more than in BYTE mode.
5. **Calibration is read from the scope, not computed** — in MAX/Stop YINCrement /
   YORigin have no closed form (§3.28.9/.10); voltage uses the literal guide formula
   `(raw − YORigin − YREFerence) * YINCrement`. The scope's YINCrement/YORigin are
   defined for the format in effect, so they must be queried **after** setting WORD.
6. **Time axis anchored to the trigger, spacing == the panel-derived rate** —
   `t = XORigin + (i − XREFerence)*XINCrement`, where `XINCrement == 1/SRATe` is read
   from the scope, so the sample spacing is exactly what the front-panel Mem Depth +
   timebase produced.
7. **Hard failure, never silent truncation** — a short/partial read raises, it does
   not return a quietly-shortened array. (Accuracy > availability.)

The software is **fully passive on rate/depth**: it never sends `:ACQuire:MDEPth`,
`:ACQuire:SRATe`, or `:TIMebase:*` writes. Everything keys off `:ACQuire:MDEPth?`
and the scope's own `:WAVeform:X*?` queries, which already reflect whatever the user
set on the panel. There is **no rate/depth-setting code** and there must not be one —
adding it would contradict the confirmed "set it on the scope directly" contract.

---

## 3. Gap analysis — where the current code can still violate the contract

The strategy is right; these are the edges where it can still return wrong-but-
silent data or fail a slow/large acquisition. Ordered by accuracy risk.

### 3.1 🔴 Partial read can be reported as success path-dependently
`_read_full_waveform` ([dho800.py:331]) `break`s on a zero-byte chunk and returns
the partial record; `read_channel` *then* raises "incomplete waveform" ([dho800.py:432]).
Good — but the break is the **only** stop condition for no-progress. A *transient*
empty `:WAV:DATA?` (scope momentarily busy on a deep/slow acquisition) is treated as
terminal with **no retry**, turning a recoverable stall into a failed shot. → Add a
bounded retry (N attempts with backoff) on an empty chunk before giving up; only
then break. Still raise if the record is genuinely short.

### 3.2 🔴 No read-back of the firmware's accepted STARt/STOP window
History (`2bf5e61`) shows firmware can clamp the window near the depth boundary. MAX
mode fixed the known case, but there is no *defensive* check that the window the
firmware actually used matches what we asked for. → After writing STARt/STOP near
the `MDEPth` boundary, optionally read `:WAVeform:STARt?/:STOP?` back once and raise
on mismatch. Cheap insurance against a future-firmware regression silently shifting
the record.

### 3.3 🟡 Per-transfer cap is learned, not asserted
`window = got` adopts the firmware's observed cap from the first chunk
([dho800.py:360]). If the first chunk is anomalously short (busy scope), every
subsequent window is undersized — still *correct* (consecutive), just slow. Accuracy
is safe; throughput isn't. → Acceptable, but log the learned cap in verbose mode so
a pathological 1-byte cap is visible.

### 3.4 🟡 Timeout scaling unproven at the 25M ceiling
`_waveform_read_timeout` ([dho800.py:326]) is `max(15, 5 + window//1e6*5)`. This is
a *per-chunk* timeout; the per-transfer cap is usually well below 25M, so a 25M read
is **many** chunks and the *total* wall-clock time is the real concern. The current
formula gives only ~15 s for a sub-1M chunk regardless of how many chunks follow —
fine per-chunk, but nothing bounds the aggregate, and a deep record at a low sample
rate can sit in firmware longer than the linear-in-points model predicts (the
trailing-newline pause noted in `rigol_functions.command`). At **25M** there are also
~5× more chunks than the earlier 5M estimate, **and WORD format doubles the bytes per
point** (50 MB/channel vs 25 MB), so the transfer is ~2× longer again — any per-chunk
stall compounds across more, larger chunks. → **Bench-measure the actual full 25M
WORD read time** on 00.01.05, then set the per-chunk floor/scaling (and any
consumer-side overall cap, e.g. interf's `RIGOL_OPERATION_TIMEOUT`) from the observed
number with margin; don't guess. The per-chunk timeout should also key off *bytes*,
not points, now that bytes ≠ points. This is the §5.2 deep-record test.

### 3.5 🟡 `screen_png` mode mismatch (data integrity of the screenshot only)
`:DISPlay:DATA?` is sent bare but the guide default is BMP, while the code demands
`\x89PNG` ([dho800.py:503]). Not waveform-accuracy, but it's a latent "works by luck
of firmware default" path. → Send `:DISPlay:DATA? PNG` explicitly — **only after
confirming on hardware** what bare returns on this firmware.

### 3.6 🟢 `get_memory_depth` silent fallback to 12000
([rigol_functions.py:192]) On a failed `:ACQuire:MDEPth?` it returns `12000` — a
magic depth that would make `read_channel` read the *wrong length and call it
success*. Directly violates contract (1). → For the accuracy path, `memory_depth()`
should **raise** on an unreadable depth rather than fabricate one (the driver's
`RigolScopeError` wrapper already exists at [dho800.py:301]; ensure the fallback
can't mask it).

### 3.7 🟡 Probe-ratio calibration unverified on 00.01.05 (firmware-jump risk)
The changelog shows probe-ratio handling changed in exactly the versions between the
old validated 00.01.03 and the new target 00.01.05:
`00.01.04.00.01` *"Fixed incorrect trigger and decode threshold values when vertical
unit was set to V with probe ratio applied"* and `00.01.05.00.00` *"Fixed the issue
where the UI did not synchronize after setting probe ratio units via SCPI."* The
driver reads `:CHANnel<n>:SCALe?`/`:OFFSet?` for the calibration sanity check
(`_sanity_warnings`, [dho800.py:476]) and HDF5 metadata. → On 00.01.05, **bench-verify
that scale/offset read back correctly with a real probe ratio applied** (e.g. 10×),
and that the converted voltage matches a known input amplitude. The waveform Y-scaling
itself comes from `:WAVeform:YINCrement?` (independent of this), so this is a
context/sanity-metadata risk, not necessarily a sample-accuracy one — but confirm.

### 3.8 🔴 BYTE-only read discards 4 of 12 ADC bits — WORD is now REQUIRED
The DHO800 has a **12-bit ADC**, but the driver reads `:WAVeform:FORMat BYTE` (8-bit),
discarding the low 4 bits → vertical resolution capped at 1/256 instead of 1/4096.
The user requires **12-bit acquisition always**, so this is no longer a deferred
enhancement — **it is a blocking contract item.** WORD (2 bytes/pt, §3.28.3) is fully
specified by the guide; the driver currently *rejects* it at runtime ([dho800.py:392])
and the read path hardcodes BYTE in three places that must all change:

- **The format guard** ([dho800.py:392]) — allow `WORD`.
- **`_read_full_waveform` byte↔point accounting** ([dho800.py:331]) — it assumes
  `1 byte == 1 point` (`window = got`, `start += got`, the `n_total` cap are all in
  *points* but compared against *byte* counts). With WORD, 1 point == 2 bytes, so the
  STARt/STOP windows (which are in **points**) and the returned **byte** lengths must
  be reconciled: `points_got = bytes_got // 2`, advance `start` by points, size the
  window cap in points. An off-by-factor-of-2 here silently halves or doubles the record.
- **The dtype parse** ([dho800.py:437]) — `np.frombuffer(payload, dtype=np.uint8)`
  becomes `dtype='<u2'` (little-endian uint16). **Byte order must be confirmed on
  hardware** (guide implies LSB-first; verify). A wrong endian turns every sample into
  garbage that still *looks* plausible — so the §5 ramp/known-amplitude checks are how
  we catch it.

Calibration: query `:WAVeform:YINCrement?/YORigin?/YREFerence?` **after** `:FORMat WORD`
so the scope reports the WORD-mode scaling; the conversion formula
`(raw − YORigin − YREFerence) * YINCrement` is unchanged, only `raw` is now uint16.

**Data-size impact:** WORD doubles every transfer. At the 25M ceiling that's **50 MB
raw/channel** and ~400 MB once float64-promoted for two channels per shot — feeds
directly into the §3.4 timeout and the §5.2 memory-footprint check.

---

## 4. Canonical acquisition sequence (the target, end to end)

This is what every consumer (`interf_main`, `dimag_main`, `test_rigol_acquire`)
should drive — unchanged in shape from today, with the §3 hardening folded in:

```
# 1. Arm and capture a real trigger
:SINGle                         # (or rely on the consumer's RUN/NORM setup)
wait_until_stopped()            # blocking poll on :TRIGger:STATus? == STOP  (c766479)

# 2. Select source and the full-depth, full-resolution read mode
:WAVeform:SOURce CHANnel<n>
:WAVeform:MODE   MAXimum        # Stop-state => full internal record (§3.28.2)
:WAVeform:FORMat WORD           # 2 bytes/pt, full 12-bit code (§3.28.3) — REQUIRED

# 3. Authoritative length + calibration, read ONCE from the scope (AFTER :FORMat WORD)
N = :ACQuire:MDEPth?            # the only true sample count (§3.3.2) — raise if unreadable
:WAVeform:XINCrement? XORigin? XREFerence?
:WAVeform:YINCrement? YORigin? YREFerence?    # WORD-mode scaling

# 4. Batched, strictly-consecutive read over 1..N  (STARt/STOP are in POINTS)
for windows of (learned cap, in points) until N:
    :WAVeform:STARt k ; :WAVeform:STOP k+w-1
    [optionally near boundary: read back STARt?/STOP? and assert]
    block = :WAVeform:DATA?    # binary, IAC-bypassed; retry on transient empty chunk
    points_got = len(block) // 2   # WORD: 2 bytes per point
    start += points_got            # advance in POINTS, not bytes
# raise if total points < N  (never return a short array as success)

# 5. Parse + convert (WORD: little-endian uint16, verify endian on hardware)
raw     = frombuffer(payload, dtype='<u2')        # 12-bit code in 16-bit word
voltage = (raw - YORigin - YREFerence) * YINCrement
time    = XORigin + (i - XREFerence) * XINCrement
```

No software rate/depth control appears anywhere: steps 3–5 read the scope's own
depth and X/Y parameters, so the *same code* is correct for whatever Mem Depth the
user dialed in on the panel — the driver only ever reads.

---

## 5. Verification plan — make "acquired vs. screen" checkable (the real deliverable)

This is the section that addresses the actual pain: *"the acquired data being
different from the screen makes testing difficult."* The fix is not to change what's
acquired (full record stays), but to make the **screen window a locatable slice of
the full record** so a bench check is a clean overlay, not guesswork.

Per the standing practice that hardware/scope code is **bench-tested by the user**,
the work below is "make the checks exist and be runnable," not "assert it passes."

### 5.1 The one new derived quantity: the on-screen window within the full record
The screen shows `screen_span = timebase_scale × 10` seconds centered on the trigger.
Given the full record's own time axis, the on-screen samples are exactly:

```
screen_lo_t = timebase_offset − screen_span/2      # left graticule edge (s, rel. trigger)
screen_hi_t = timebase_offset + screen_span/2      # right graticule edge
i_lo = searchsorted(time, screen_lo_t)             # index into the full record
i_hi = searchsorted(time, screen_hi_t)
screen_slice = voltage[i_lo:i_hi]                  # what the scope draws
```

Have the driver/test **report `(i_lo, i_hi, screen_lo_t, screen_hi_t, len(screen_slice))`**
alongside the full record. Now "does the acquired data match the screen?" becomes:
*overlay `screen_slice` on a screenshot* — exact, not eyeballed against the whole array.
(`screen_png` already exists for the screenshot side.)

### 5.2 Extend `test_rigol_acquire.py` (on firmware 00.01.05, depth-stepped to 25M)
It already checks `len==MDEPth`, finiteness, non-clipping, t=0 anchoring. Add — and
run the whole thing **stepping Mem Depth up to the 25M ceiling** (e.g. set 1k / 10k /
100k / 1M / 10M / 25M on the panel between runs, or accept a `--expect-mdepth` arg
per run):

1. **Full-record length.** `len(voltage) == :ACQuire:MDEPth?` exactly (contract 1),
   for whatever Mem Depth is set on the panel — print both so a mismatch is obvious.
   **No rate sweep** (rate is panel-fixed, not under test); record the panel's depth +
   resulting SRATe in the output. Confirm 25M reads back as `:ACQuire:MDEPth? ≈ 25e6`.
2. **Screen-slice extraction + report** (§5.1). Print the window indices/times and
   plot the full record with the on-screen slice highlighted, so the
   acquired-vs-screen relationship is visible in one figure — *the fix for the
   "acquired ≠ screen makes testing hard" complaint.*
3. **No padding / no truncation.** Existing trailing-constant-run tripwire (catches
   zero-pad); plus assert the last batch reached exactly `N` (no silent short read).
   Most important at 25M, where the multi-chunk batch loop does the most work.
4. **Duplicate/gap detector.** Feed a non-repeating ramp across the full window;
   assert monotonic / no repeated-block signature (the exact RAW failure in `19dff28`).
   At 25M this exercises *the most* `:WAVeform:STARt/STOP` windows — the prime place a
   chunk-boundary bug would surface.
5. **WORD / 12-bit integrity** (the headline accuracy requirement). Confirm:
   - `:WAVeform:FORMat WORD` is set and the read returns **2 bytes/point**
     (`declared_bytes == 2 × MDEPth`).
   - parsed as `<u2`, codes span the **12-bit** range (0..4095, not 0..255) — feed a
     ramp/sine and check the histogram uses >256 distinct codes (proves you're not
     silently getting 8-bit data).
   - **endian check:** a smooth input must yield a smooth `raw` array; a byte-swap
     shows up as high-frequency garbage. Compare against the same trace in BYTE to
     confirm WORD ≈ BYTE×16 in code space (sanity), then keep WORD.
6. **0xFF / 0xFFFF integrity.** Drive a channel so sample bytes hit 0xFF in *both*
   halves of a 16-bit word; assert they appear (guards the IAC-bypass path, which
   matters more in WORD since either byte can be 0xFF).
7. **25M WORD deep-record timing + timeout.** Measure and print the wall-clock time of
   the full 25M WORD read; confirm it completes without a `:WAV:DATA?` timeout. Feed
   the measured time back into the §3.4 per-chunk timeout *and* any consumer-side
   overall cap (set from data, not guessed). **Memory note (WORD):** 25M × uint16 =
   ~50 MB raw/channel, ~**400 MB** once promoted to float64 voltage+time for two
   channels — confirm the test (and the real consumers + HDF5 writers) handle that
   per-shot footprint.
8. **Probe-ratio calibration sanity** (§3.7, 00.01.05-specific). With a 10× probe
   ratio applied, confirm `:CHANnel<n>:SCALe?`/`:OFFSet?` read back correctly and the
   converted voltage matches a known input amplitude.

Record the firmware version (`*IDN?`) and the panel Mem Depth in every test run's
output — every finding here is firmware-specific, and this suite is establishing
**00.01.05** as the new validated baseline (replacing 00.01.03; see §8).

---

## 6. Sequenced work items

Pre-work (user, hardware): **upgrade one scope 00.01.02 → 00.01.05**, confirm the
deeper Mem Depth options (up to the **25M ceiling**) are now available on the panel.
All bench tests below run on that scope.

Static/code changes (syntax-only verification by me; user bench-tests):

1. **WORD / 12-bit read path** (§3.8) — **the headline requirement.** Allow `WORD` in
   `read_channel`; rework `_read_full_waveform` so STARt/STOP windows and `start`
   advance in *points* while the returned lengths are *bytes* (`points = bytes // 2`);
   parse `<u2`; query Y-calibration after `:FORMat WORD`. The largest and most
   accuracy-critical change — everything else verifies *this* read.
2. **`memory_depth()` must not fabricate a depth** (§3.6) — make the accuracy path
   raise on unreadable `:ACQuire:MDEPth?`. *Smallest change, high accuracy impact.*
3. **Screen-window slice helper** (§5.1) — returns `(i_lo, i_hi, screen_lo_t,
   screen_hi_t)` of the on-screen window within the full record, from
   `timebase_scale`/`timebase_offset` + the record's time axis. Makes "acquired vs.
   screen" directly checkable. *Primary verifiability feature.*
4. **Per-chunk timeout keyed off bytes, not points** (§3.4) — required once WORD makes
   bytes ≠ points; fold in the measured 25M WORD read time.
5. **Firmware gate in the test harness** — print `*IDN?` firmware up front; flag if
   it isn't the validated **00.01.05** so every bench run self-documents its firmware.
6. **Bounded retry on transient empty chunk** in `_read_full_waveform` (§3.1) —
   matters most at 25M WORD, where the batch loop runs the most/largest transfers.
7. **Optional STARt/STOP boundary read-back + assert** (§3.2), verbose-gated so it
   adds no cost to the hot path unless enabled.
8. **Log the learned per-transfer cap** in verbose mode (§3.3).
9. **In-code comment recording the MAX-vs-RAW decision** near `read_channel` so it
   stops getting re-flagged (already noted as pending in the investigation doc).
10. **`test_rigol_acquire.py`: WORD/12-bit + full-record + screen-slice + 25M deep-read
   checks** (§5.2) — the verification deliverable; depth-stepped to 25M in WORD, plots
   the full record with the on-screen slice highlighted, measures the read time.
11. **Probe-ratio calibration sanity check** (§3.7) — 00.01.05-specific; verify
   scale/offset read-back + converted amplitude with a 10× probe applied.

Deferred (own bench-validation each):
- `screen_png` PNG arg (§3.5) — confirm bare-`:DISPlay:DATA?` behavior first.

Explicitly **not** in scope: any return to RAW mode; any code that *sets* sample
rate, memory depth, or timebase (the user sets those on the panel); any trimming of
the returned record to the screen window (full record is the contract); keeping BYTE
as the acquisition format (12-bit/WORD is now required).

---

## 7. Open questions for the user (block nothing; refine §5/§6)

*(All open questions resolved as of 2026-06-19.)*

- **Vertical resolution:** **12-bit always required** → WORD format is mandatory
  (work item 1), not deferred.
- **Depth ceiling:** **25M** (see §8).
- **Fleet rollout:** 00.01.05 to all scopes after the one-scope validation passes is
  **approved**.

---

## 8. Firmware decision & changelog evaluation (2026-06-19)

**Target: 00.01.05** (`00.01.05.00.02`, current Rigol release). Validate on one scope,
then **roll out fleet-wide** once it passes — *approved by the user 2026-06-19.*

**Why upgrade at all — the only concrete win is deeper Mem Depth.** Reviewed the full
DHO800/DHO900 release notes from 00.01.02 → 00.01.05. **No** entry across any version
touches the driver's actual path: nothing on `:WAVeform` reading (DATA/STARt/STOP/
POINts/PREamble), MAXimum/RAW mode, `:ACQuire:MDEPth`, sample rate, or the binary-
block/LXI transport. So upgrading does **not** fix or improve the existing full-record
read, and does **not** enable a return to RAW mode. The one relevant feature is the
added memory-depth options — `00.01.03.00.04`: *"Added 5M memory depth option for
DHO800 series"* (and later steps up to the **25M** working ceiling the user targets).
This is the sole reason the upgrade is worthwhile, and it directly serves the
full-memory-depth goal. (`00.01.04.00.00` adds a 50M option for DHO824 specifically;
the fleet ceiling here is 25M.)

**Why this is a *fresh* validation, not a carry-over from 00.01.03.** Probe-ratio
handling changed in the versions between the old validated 00.01.03 and 00.01.05
(`00.01.04.00.01` trigger/decode threshold with probe ratio; `00.01.05.00.00` SCPI
probe-ratio UI sync). The driver reads `:CHANnel<n>:SCALe?`/`:OFFSet?`, so 00.01.05
gets its own probe-ratio calibration check (§3.7, work item 11). After validation,
update the `rigol_dho800_investigation.md` "validated on" line from 00.01.03 to
00.01.05.

**Decided contract (from the discussion):**
- **Rate/depth set on the scope, not in software** → driver stays read-only on
  rate/depth/timebase. (Earlier "set sampling rate remotely" idea is rejected.)
- **Return the full Mem Depth record**, not the screen window → no trimming; the
  acquired-vs-screen difference is expected and made verifiable via the §5.1 slice.
- **"At least the screen length"** is automatically satisfied because MAX/Stop's full
  record ⊇ the screen window.
- **25M is the deep-record test ceiling** (real working max for the DHO800 fleet);
  upgrade one scope to 00.01.05 to reach the deeper depths, then roll out fleet-wide.
- **12-bit acquisition is required** → `:WAVeform:FORMat WORD` is mandatory; BYTE
  (8-bit) is no longer an acceptable acquisition format. (Resolved 2026-06-19.)

---

## 9. Manual bench procedure — probe-ratio calibration (gap 3.7, firmware 00.01.05)

No driver code change (gap 3.7 is bench-only). Probe-ratio handling changed between
the old validated 00.01.03 and the new 00.01.05 (changelog `00.01.04.00.01`,
`00.01.05.00.00`), and the driver reads `:CHANnel<n>:SCALe?`/`:OFFSet?` for the
calibration sanity check and HDF5 metadata. Run this once on the upgraded unit to
confirm scale/offset still read back correctly with a real probe ratio applied.

Setup: feed a channel a **known amplitude** (e.g. a 1.000 Vpp square/sine from a
function generator), set that channel's **probe ratio to 10×** on the front panel,
choose a V/div that keeps the trace on screen.

1. Read back the channel scale/offset and confirm they reflect the 10× ratio:
   ```python
   from lab_scopes.rigol import RigolDHO800
   with RigolDHO800("<scope-ip>", verbose=True) as s:
       print("V/div :", s.vertical_scale(1))     # should match the panel V/div readout
       print("offset:", s.vertical_offset(1))
   ```
   Both must agree with what the front panel shows (the probe ratio is applied).
2. Acquire and check the converted amplitude matches the known input:
   ```python
       s.single(); s.wait_until_stopped()
       w = s.read_channel(1)                       # WORD / 12-bit
       print("Vpp:", float(w.voltage.max() - w.voltage.min()))
   ```
   The measured Vpp should equal the generator's amplitude (within probe/scope
   tolerance). A result off by exactly the probe ratio (10× or 1/10×) means the
   scale read-back is not honouring the probe ratio on this firmware — record it and
   flag before trusting 10× probe metadata.
3. Note the result (pass / off-by-ratio) in the validation log alongside the firmware
   version.

This procedure is duplicated in the docstring of `tests/test_rigol_scope_real.py`
(see §6 work item 10) so it travels with the test suite.
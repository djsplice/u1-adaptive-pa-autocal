# U1 Adaptive PA AutoCal — Coil Data Capture

Optional coil frequency capture and waveform analysis.
Calibration **does not require** this — console `area` lines alone are enough for `APA_FINISH_*` and Orca rows.
Day-to-day steps: [USER_GUIDE.md](USER_GUIDE.md). Residual math: [METHODOLOGY.md](METHODOLOGY.md).

Optional capture is useful for learning, debugging sign-flips, and checking that residual matches what you see on the scope. The tool is:

[`scripts/coil_dump_client.py`](../scripts/coil_dump_client.py)

This records **coil oscillation frequency (Hz)** as a back-pressure / filament-motion proxy — **not** Prusa-style loadcell force in grams.

## Two independent data paths

APA macros and the dump client do **different** jobs in parallel:

```text
coil_dump_client.py                    APA_COIL_* macros
        │                                      │
        ▼                                      ▼
FREQUENCY_MEASURE PROBE=…              FLOW_MEASURE_K / FLOW_CALIBRATE
  (one long external buffer)             (short internal client per K)
        │                                      │
        ▼                                      ▼
gcodes/frequency_data/                 Console: k0.010: area: 1278
  frequency-<sensor>-<name>.csv        Optional per-K dumps under
  (scope plot of whole session)          frequency_data/flow_test/…
```

| Source | Created by | Use |
|--------|------------|-----|
| `k0.xxx: area: …` in Fluidd console | APA / `FLOW_MEASURE_K` | **Required** for $K^*$ and Orca rows |
| `frequency-<sensor>-<name>.csv` | `FREQUENCY_MEASURE` via dump client (or manual G-code) | Optional full-session waveform |
| `freq-k0.xxxxx.csv` under `flow_test/` | Stock measure path (when enabled) | Optional per-K short dumps |

The long named file (e.g. `frequency-extruder2-apa_test1.csv`) is **not** written by `adaptive_pa_macro.cfg`. It only appears if you start `FREQUENCY_MEASURE` (the dump client does this for you).

## Laptop capture via Moonraker (recommended)

From a machine that can reach the printer (deps: `numpy`, `matplotlib` — a small venv is fine):

```bash
# Terminal 1 — start capture first (wait-for-Enter mode)
python3 scripts/coil_dump_client.py \
  --moonraker http://PRINTER_IP \
  --sensor extruder2 \
  --name apa_center_t2

# Terminal 2 / Fluidd — run the test while capture is armed
APA_COIL_CENTER EXTRUDER=2 MODE=MEASURE TEMP=220
```

When the last `k0.xxx: area:` line has printed and the point finishes, press **Enter** in Terminal 1. The client sends:

```gcode
FREQUENCY_MEASURE PROBE=extruder2 NAME=apa_center_t2
```

and downloads:

```text
gcodes/frequency_data/frequency-extruder2-apa_center_t2.csv
→ local coil_extruder2_apa_center_t2.csv (or --csv path)
```

**Timing tips**

| Scope | Wall-clock to leave capture armed |
|-------|-------------------------------------|
| One MEASURE cell (default 7 $K$ steps) | ~5–7 minutes after extrude starts (~300–400 s of samples is normal) |
| Full `APA_COIL_RUN_ALL` (5 points) | ~25+ minutes — prefer Enter at suite end, not a short `--duration` |

Optional auto-stop:

```bash
python3 scripts/coil_dump_client.py \
  --moonraker http://PRINTER_IP \
  --sensor extruder2 \
  --name apa_center_t2 \
  --duration 400
```

## Partial download warning (async write)

Stock `FREQUENCY_MEASURE` finishes the G-code **before** the CSV is fully flushed: `write_to_file()` starts a **background** process. If the client downloads too early, you get a **truncated** local file (same start timestamp, shorter end time / fewer lines) while the **on-printer** file is complete.

**If your laptop CSV looks short:**

1. Open Fluidd → **Files** → `gcodes/frequency_data/`
2. Download `frequency-<sensor>-<name>.csv` again (full size)
3. Plot offline:

```bash
python3 scripts/coil_dump_client.py --csv-in frequency-extruder2-apa_center_t2.csv
```

The dump client now waits longer after stop before downloading; still prefer the printer copy if durations look wrong.

## What one MEASURE cell looks like on the scope

Default scan (`k_min=0.005`, `k_max=0.040`, `k_step=0.005`, exclusive max) runs **7** candidate $K$ values:

$$0.005,\ 0.010,\ 0.015,\ 0.020,\ 0.025,\ 0.030,\ 0.035$$

Each $K$ runs `LOOP=14` pure-E **slow↔fast** cycles (`_extrude_loop`), then a short trail / move / prep gap before the next $K$.

![Full MEASURE sweep: seven K blocks as comb-shaped frequency drops, high plateaus between them](images/coil-full-k-sweep.jpg)

*Example: full `APA_COIL_CENTER` on extruder2. High idle → seven “combs” (one per $K$) → post-test spikes. Tall towers between combs are inter-$K$ gaps, not extra PA values.*

### Hierarchy (easy to misread)

| Structure | Meaning |
|-----------|---------|
| One **comb / block** (~30–40 s of activity) | **One candidate $K$** |
| One **tooth** inside the comb (×14) | One slow+fast **loop** at that fixed $K$ |
| Fine hash on a plateau | Mid-cruise noise; secondary for PA |
| **Corners** at speed changes | Primary PA residual (over/undershoot) |

![Single K block: fourteen slow/fast loops labeled 1–14](images/coil-single-k-14-loops.jpg)

*Zoom on one $K$: fourteen slow/fast cycles. These are **not** fourteen different $K$ values.*

## How residual (`area`) shows up on the waveform

Firmware `area` is a **signed residual** over transition windows (aligned with accel/cruise timing), not “how tall is the square wave.”

* The large high↔low step is mostly **slow flow vs fast flow** (present at every $K$).
* PA quality lives in the **edges** (corners into/out of each cruise level): overshoot spikes, undershoot notches, settle time, and whether that bias is **systematic** across loops.
* On a single loop you often see **both** an overshoot on one transition and an undershoot on the other; `area` integrates those errors with a sign.

| `area` | Meaning | Typical edge look |
|--------|---------|-------------------|
| $\gg 0$ | Under-PA ($K$ too low) | Soft/laggy landings; residual one sign |
| $\approx 0$ | Near optimal | Corners land on the next plateau without big spikes or deep Vs |
| $\ll 0$ | Over-PA ($K$ too high) | Sharp **overshoot** past the plateau and/or deep **undershoot** before settle |

### Edge windows (what residual cares about)

![Near-optimal edges at k=0.015: orange boxes on rise and fall corners](images/good-PA.png)

*Zoom on one slow/fast cycle near $K \approx 0.015$. Orange boxes mark the **transition windows** — entry onto the high cruise and exit toward the low cruise. Clean corners here mean little extra miss past the plateau levels; mid-plateau hash is normal and secondary.*

![Over-PA at k=0.025: labeled Over spike and Under notch](images/high-PA.png)

*Same style of zoom at $K = 0.025$ (**too much PA**). **Over:** rising edge shoots above the high plateau, then settles down. **Under:** falling edge plunges below the low cruise before recovering. Both are over-PA edge artifacts on opposite transitions; large negative `area` is the integral of that pattern over ~14 loops.*

### How edges get worse as $K$ increases (same test)

![Three K blocks: 0.015 good, 0.020 slightly over, 0.025 clearly over](images/coil-k010-k015-compare.png)

*Three consecutive MEASURE blocks from one center-point capture (extruder2). Left→right, $K$ steps up and edge drama grows:*

| Block | Console (example center run) | Waveform cue |
|-------|------------------------------|--------------|
| $k = 0.015$ | `area: -6811` (just past zero-cross) | Most regular teeth; peaks/troughs look **good** by eye |
| $k = 0.020$ | `area: -19159` | Slightly deeper troughs / sharper corners (**slightly over**) |
| $k = 0.025$ | `area: -27580` | Obvious overshoot and deep undershoot (**clearly over**) |

On that same run the **sign flip** was earlier:

```text
k0.010: area: +1278    ← still slightly under-PA
k0.015: area: -6811    ← first negative (mild over-PA)
→ K* ≈ 0.011
```

So “**good**” on the $0.015$ comb means *best-looking of the over-PA side / closest of these three to optimal*, not `area = 0`. The integral already flipped negative at $0.015$; zero-cross interpolation between $0.010$ and $0.015$ is still the right $K^*$. Eyeballing alone often lands near the flip but can sit one step high — trust the console numbers for Orca rows.

**Reading checklist (sanity-check, not a substitute for `area`):**

1. Identify the seven combs and match them to console `k` lines in time order.
2. Zoom a mid-block loop (skip the first 1–2 teeth after a $K$ change).
3. On **both** edges of a tooth: clean land on the next plateau, or Over spike / Under V?
4. As $K$ rises past the flip, expect troughs and corner spikes to get more extreme (as in the three-block figure).
5. Confirm the first **positive→negative** `area` pair sits near the combs that look least pathological.

Official confidence scoring in `APA_FINISH_*` still uses **only** the `area` numbers (SNR, bracket balance, clean zero) — not cycle-to-cycle variance from the CSV. See [METHODOLOGY.md](METHODOLOGY.md#signal-quality--confidence-scoring-algorithm). Waveforms are for understanding and debugging.

## Other connection modes

```bash
# On the printer host (or SSH -L tunnel to klippy.sock) — live dump stream
python3 scripts/coil_dump_client.py \
  --uds ~/printer_data/comms/klippy.sock \
  --sensor extruder2

# Offline replot only
python3 scripts/coil_dump_client.py --csv-in path/to/frequency-extruder2-apa_center_t2.csv
```

Many U1 / nginx setups do **not** expose `/klippysocket` for bulk dumps from a laptop. Prefer `--moonraker` + `FREQUENCY_MEASURE` (HTTP) as above.

## Capture troubleshooting

| Symptom | Cause | Action |
|---------|--------|--------|
| Laptop CSV ~half the duration of the printer file | Downloaded while `write_to_file` still running | Re-download from Fluidd `frequency_data/` |
| Only 3–4 combs but console has 7 `area` lines | Truncated download or capture stopped early | Use full on-printer CSV; leave capture until last `area` line |
| Flat high line only | Capture armed but no extrusion yet / wrong sensor | Confirm `PROBE`/`--sensor` matches active tool (`extruder2` for T2) |
| No `frequency-*.csv` after APA alone | Expected — macros do not call `FREQUENCY_MEASURE` | Run dump client (or manual `FREQUENCY_MEASURE`) in parallel |
| Very long multi-suite capture looks cut off | Stock helper can stop accepting batches after many messages (memory guard) | Capture **per test point**, or use short per-K `flow_test` dumps |

## Regenerating figures

Figures under [`images/`](images/) were captured from a real `APA_COIL_CENTER` + `FREQUENCY_MEASURE` session. To refresh them:

```bash
python3 scripts/coil_dump_client.py --csv-in frequency-extruder2-<name>.csv
# Save zoomed / annotated screenshots as:
#   images/coil-full-k-sweep.jpg       # full 7-K staircase
#   images/coil-single-k-14-loops.jpg  # one K, 14 loops
#   images/good-PA.png                 # clean edges near K*
#   images/high-PA.png                 # Over + Under at high K
#   images/coil-k010-k015-compare.png  # multi-K progression (name is historical)
```


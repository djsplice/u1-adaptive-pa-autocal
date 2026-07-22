# U1 Adaptive PA AutoCal — User Guide

Macros in [`adaptive_pa_macro.cfg`](../adaptive_pa_macro.cfg) help build an **OrcaSlicer Adaptive Pressure Advance** table using the Snapmaker U1's stock `FLOW_MEASURE_K` command (pure-E velocity steps + toolhead flow residual). **No Klipper Python changes are required.**

Rather than slicing, printing, and visually scoring **5 to 10+ calibration prints** per toolhead (as recommended in [OrcaSlicer's Adaptive PA guide](https://github.com/OrcaSlicer/OrcaSlicer/wiki/adaptive_pressure_advance_calib)), these macros streamline calibration in two key ways:

1. **Automated Sensor Measurement:** Uses the U1's inductance flow sensor to measure extrusion pressure residuals directly in a non-printing test run (eliminating bed heating, printed line inspection, and manual scoring).
2. **Reduced Test Count (Box-Style Methodology):** Uses Box-style response surface sampling (4 envelope corners plus 1 center point) to accurately map the full operating envelope in just 5 smart test points instead of an exhaustive grid search.

For a 4-toolhead U1, this replaces **20 to 40+ individual calibration prints** with a fast, sensor-driven process to generate your OrcaSlicer table rows:

```text
PA, flow_mm3_s, accel_mm_s2
```

---

## 1. Prerequisites

Before running the calibration, ensure:

1. **Working Flow Sensor:** U1 with `[flow_calibrator]` and operational inductance-based flow sensing.
2. **Loaded Filament & Clean Nozzle:** Target filament loaded on the tool you wish to test, nozzle clean of debris for purges.
3. **Idle Printer at Printing Temp:** Machine idle (not printing) heated to your filament's standard printing temperature (e.g., PLA at 220 °C).
4. **Include Macro File:** Add the macro to `printer.cfg` and restart Klipper:

```ini
[include adaptive_pa_macro.cfg]
```

---

## 2. Quick Start Guide (3-Step Walkthrough)

Calibrating a toolhead takes about 25 minutes using the automated 5-point test suite.

### Step 1: Run the automated test suite

To run the complete 5-point calibration suite on a single toolhead, execute:

```gcode
APA_COIL_RUN_ALL EXTRUDER=0 TEMP=220
```

*(Or run all toolheads sequentially: `APA_COIL_RUN_ALL_TOOLS TOOLS=0,1,2,3 TEMP=220`)*

The printer automatically selects the specified tool, heats the nozzle to temperature, and runs the entire 5-point test suite in one continuous pass.

---

### Step 2: Read the log output and run finish commands

After `APA_COIL_RUN_ALL` completes, review your console log. Each of the 5 test points prints a block of `k` values followed by a `MEASURE DONE` banner.

For each test point block:
1. Find the pair of lines where `area` **flips from positive to negative**:

```text
k0.010: area: 12450
k0.015: area: 5128
k0.020: area: -5281
k0.025: area: -11800
```

2. Copy the template command printed in that test point's `MEASURE DONE` banner (which includes `FLOW`, `ACCEL`, and `NAME`) and fill in `K0`, `A0`, `K1`, and `A1`:

```gcode
APA_FINISH_CELL K0=0.015 A0=5128 K1=0.020 A1=-5281 FLOW=8.14 ACCEL=2000 NAME=low_anchor MAX_ABS=12450
```

*(Note: If you run test points individually or process the very last point immediately after it finishes, you can use the shortcut `APA_FINISH_LAST K0=0.015 A0=5128 K1=0.020 A1=-5281`. When reviewing prior points in the log after the full suite finishes, use `APA_FINISH_CELL` as shown above).*

*Tip: `area` flips from positive (K too low) to negative (K too high). `MAX_ABS=` takes the largest absolute sensor magnitude from that sweep (always entered as a positive number, e.g., `MAX_ABS=12450`), which allows the macro to grade signal-to-noise ratio. Scoring details: [METHODOLOGY.md](METHODOLOGY.md#signal-quality--confidence-scoring-algorithm).*

---

### Step 3: Copy the line into OrcaSlicer

`APA_FINISH_LAST` calculates the exact zero-crossing Pressure Advance ($K^*$) and outputs a paste-ready row:

```text
========== APA FINISH: low_anchor ==========
Bracket: k=0.015 area=5128  ->  k=0.020 area=-5281
K* (zero cross): 0.01742  ->  rounded 0.017
Confidence: 84/100 (excellent) | good signal | bracket_balance=0.985 | clean zero
--- copy/paste into Orca Adaptive PA ---
0.017, 8.14, 2000
----------------------------------------
```

Repeat **Step 2** for each of the 5 test points. Copy all 5 output rows into OrcaSlicer:

**Filament Settings $\rightarrow$ Enable adaptive pressure advance $\rightarrow$ Adaptive pressure advance measurements**

---

## 3. Presets & Envelope Tuning

Default geometry and speed/acceleration envelopes are configured in `_APA_COIL_CFG`:

| Parameter | Default |
|-----------|---------|
| Nozzle / Layer Height / Line Width | 0.4 / 0.2 / 0.45 mm |
| Filament Diameter | 1.75 mm |
| Line Area ($A_{\text{line}}$) | 0.081416 mm² |
| Flow ($Q = v_{\text{XY}} \cdot A_{\text{line}}$) | Calculated automatically |
| Speeds (low / mid / high) | **100 / 200 / 250** mm/s |
| Accels (low / mid / high) | **2000 / 6000 / 10000** mm/s² |

The 5 test points evaluate your machine across this operating envelope:

| Test Point | XY Speed | XY Accel | Volumetric Flow ($Q$) | Role |
|------------|----------|----------|-----------------------|------|
| `low_anchor` | 100 mm/s | 2000 mm/s² | ~8.1 mm³/s | Low flow at HQ outer wall acceleration |
| `high_flow` | 250 mm/s | 2000 mm/s² | ~20.4 mm³/s | High flow at soft acceleration |
| `high_force` | 100 mm/s | 10000 mm/s² | ~8.1 mm³/s | Low flow at inner wall / infill acceleration |
| `stress` | 250 mm/s | 10000 mm/s² | ~20.4 mm³/s | Combined high flow + high acceleration |
| `center` | 200 mm/s | 6000 mm/s² | ~16.3 mm³/s | Mid-point print condition |

---

### Envelope Presets

You can easily select standard envelope presets before running `APA_COIL_RUN_ALL`:

```gcode
# Standard Orca profile HQ defaults (100-250 mm/s, 2k-10k accel)
APA_COIL_PRESET_U1_DEFAULT

# Normal outer (4k) to inner (10k) acceleration profile
APA_COIL_PRESET_ORCA_NORMAL

# Quality mode: constant single acceleration (default 2000 mm/s²)
APA_COIL_PRESET_SHAPER_QUALITY ACCEL=2000

# High-flow nozzle preset (wider speed range)
APA_COIL_PRESET_HIGH_FLOW LOW_SPEED=150 HIGH_SPEED=300

# Fully custom envelope
APA_COIL_SET_ENVELOPE LOW_SPEED=120 HIGH_SPEED=280 LOW_ACCEL=4000 HIGH_ACCEL=8000
```

---

### Speed Limit Check (Filament Flow Ceiling)

Do not set `HIGH_SPEED` faster than your hotend and filament can melt. Testing faster than your hotend can flow will cause clicking, under-extrusion, or test failures.

**How to calculate your maximum safe speed:**
1. Check your filament's **Max Volumetric Speed** in OrcaSlicer (e.g., `16.3 mm³/s` for standard PLA).
2. Divide that number by **0.0814** (the standard line area for a 0.4 mm nozzle at 0.2 mm layer height).

$$\text{Max Speed (mm/s)} = \frac{\text{Max Volumetric Speed}}{0.0814}$$

*Example:*  
If your filament is rated for **16.3 mm³/s**:
$$\text{Max Speed} = \frac{16.3}{0.0814} \approx 200\text{ mm/s}$$

Set `HIGH_SPEED=200` (or lower) so your test points remain within your hotend's flow limits.

---

### Time and Filament Costs

With default envelope settings and standard scan parameters (7 steps per point):

| Scope | Time (approx.) | Filament Usage (approx., 1.75 mm PLA) |
|-------|----------------|-----------------------------------------|
| **Single test point** | ~5 minutes | ~3–5 g |
| **Full suite (1 tool, 5 points)** | ~25 minutes | ~20 g (~5 m) |
| **All 4 toolheads (`RUN_ALL_TOOLS`)** | ~1.5 to 2 hours | ~80 g |

*Tip: Pass `SKIP_STRESS=1` to `APA_COIL_RUN_ALL` if your hotend cannot sustain high speed at maximum acceleration. This saves ~5 minutes and ~4 grams of filament.*

---

### Choosing Fallback Static PA

OrcaSlicer requires a single static **Pressure advance** value to use when Adaptive PA is disabled or unmapped.

**Simple 3-Step Recipe:**
1. Collect the calculated $K^*$ values for all good test points.
2. Sort the values from smallest to largest.
3. Take the **median value** (the middle number) and enter it as your main filament Pressure Advance in OrcaSlicer.

*Example:* For $K^*$ values of `0.012, 0.013, 0.016, 0.017, 0.023`, the median is **0.016**.

---

### Advanced Operations & Custom Test Points

Re-run an individual point or perform fine-resolution scans around a suspected root:

```gcode
# Select tool
APA_SELECT_TOOL EXTRUDER=0

# Run a single test point from the envelope
APA_COIL_HIGH_FLOW MODE=MEASURE TEMP=220

# Run a custom fine-step scan around K=0.025
APA_COIL_CELL NAME=fine_flow SPEED=200 ACCEL=6000 MIN=0.020 MAX=0.030 STEP=0.002

# Format a known K directly into an Orca line without interpolation
APA_ORCA_LINE K=0.024 FLOW=16.28 ACCEL=6000 NAME=manual_entry
```

---

## 4. Troubleshooting & Command Reference

### Troubleshooting Guide

| Symptom | Probable Cause | Recommended Action |
| :--- | :--- | :--- |
| `Unknown command: "APA_COIL_1"` | Digits in macro name | Use named macro like `APA_COIL_LOW_ANCHOR`. |
| `extruder ... not activated` | Active tool mismatch | Run `APA_SELECT_TOOL EXTRUDER=n`. |
| All `area` values have the same sign | $K^*$ is outside current scan range | Widen `MIN` or `MAX` search bounds. |
| Low `area` magnitudes (~hundreds) | Weak excitation signal (low speed) | Point has weak SNR, do not overfit. |
| Stress test point clicks or skips | Hotend flow rate exceeded | Use `SKIP_STRESS=1` or lower `HIGH_SPEED`. |
| Macro template error in `.cfg` | Inline `#` character in macro body | Remove `#` from Jinja or `gcode:` blocks. |
| Coil CSV shorter than the test / missing later $K$ combs | Downloaded while async `FREQUENCY_MEASURE` write still running | Re-download full file from Fluidd `gcodes/frequency_data/` (see [COIL_DATA_CAPTURE.md](COIL_DATA_CAPTURE.md)). |

---

### Macro Command Reference

| Command | Description |
| :--- | :--- |
| `APA_COIL_RUN_ALL` | Runs full 5-point test suite on specified `EXTRUDER`. |
| `APA_COIL_RUN_ALL_TOOLS` | Runs full suite across multiple toolheads (`TOOLS=0,1,2,3`). |
| `APA_FINISH_LAST` | Interpolates $K^*$, grades confidence, and outputs Orca row using last point. |
| `APA_FINISH_CELL` | Full finish macro with explicit `FLOW`, `ACCEL`, and `NAME` parameters. |
| `APA_COIL_SHOW_ENVELOPE` | Prints current envelope speeds, accelerations, and volumetric flows. |
| `APA_COIL_SET_ENVELOPE` | Customizes envelope speeds, accelerations, or nozzle line geometry. |
| `APA_COIL_PRESET_U1_DEFAULT` | Loads default envelope (100-250 mm/s, 2k-10k accel). |
| `APA_COIL_PRESET_ORCA_NORMAL` | Loads normal outer (4k) to inner (10k) acceleration envelope. |
| `APA_COIL_PRESET_SHAPER_QUALITY` | Sets constant single acceleration for flow-only PA tuning. |
| `APA_COIL_PRESET_HIGH_FLOW` | Loads high-flow nozzle speed envelope. |
| `APA_COIL_LOW_ANCHOR` | Runs low speed x low accel test point. |
| `APA_COIL_HIGH_FLOW` | Runs high speed x low accel test point. |
| `APA_COIL_HIGH_FORCE` | Runs low speed x high accel test point. |
| `APA_COIL_STRESS` | Runs high speed x high accel test point. |
| `APA_COIL_CENTER` | Runs mid speed x mid accel test point. |
| `APA_COIL_CELL` | Custom test point bypassing envelope defaults. |
| `APA_ORCA_LINE` | Converts a known $K$ value directly into an OrcaSlicer table row. |
| `APA_SELECT_TOOL` | Selects toolhead $T0$ to $T3$ and verifies activation. |
| `APA_COIL_HELP` | Displays quick command summary in console. |

---

## Further reading

| Doc | Contents |
|-----|----------|
| [METHODOLOGY.md](METHODOLOGY.md) | Residual `area`, zero-crossing $K^*$, box-style design, confidence scoring, kinematic conversion |
| [COIL_DATA_CAPTURE.md](COIL_DATA_CAPTURE.md) | Optional coil frequency capture, plot hierarchy, reading over/under PA on waveforms |

Optional laptop scope plots are **not** required for calibration — console `area` lines are enough for Orca rows.

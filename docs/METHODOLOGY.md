# U1 Adaptive PA AutoCal — Methodology & Math

Technical reference for residual measurement, experimental design, and confidence scoring.
For day-to-day calibration steps, see [USER_GUIDE.md](USER_GUIDE.md).
For optional coil waveforms, see [COIL_DATA_CAPTURE.md](COIL_DATA_CAPTURE.md).

---

## Physical Concept & Flow Sensor Model

Standard Pressure Advance assumes a linear relationship where extruder position lead scales with nozzle velocity via a single constant $K$. In reality, melt-zone viscosity, nozzle backpressure, and extruder dynamics cause the optimal advance parameter $K(Q, a)$ to vary significantly across operating conditions.

The U1 toolhead incorporates an inductance-based flow sensor that measures dynamic filament movement. During a pure-extrusion test step (`FLOW_MEASURE_K`), the firmware applies candidate $K$ values and measures the residual error integral, reported as `area`:

* **$\text{area} > 0$:** Extruder lead is insufficient ($K$ is too low).
* **$\text{area} < 0$:** Extruder lead is excessive ($K$ is too high).
* **$\text{area} \approx 0$:** Extruder lead balances viscous drag and elasticity ($K$ is optimal for this condition).

---

## Root-Finding & Zero-Crossing Linear Interpolation

For each test point, finding $K^*$ is a 1-D root-finding problem on the residual function $\text{area}(k)$. When two consecutive test steps $(k_0, a_0)$ and $(k_1, a_1)$ satisfy $a_0 \cdot a_1 < 0$, linear interpolation determines the root $K^*$:

$$K^* = k_0 - a_0 \cdot \frac{k_1 - k_0}{a_1 - a_0}$$

This zero-crossing interpolation is performed automatically by `APA_FINISH_LAST` and rounded to 3 decimal places.

---

## Experimental Design (Box-Style Response Surfaces)

Rather than performing an exhaustive grid search over dozens of speed and acceleration combinations, these macros employ a 2x2 factorial design plus a center point (a Box-style sparse response surface):

```text
               High Accel
                   ^
  high_force (100, 10k)     stress (250, 10k)
                   +---------------+
                   |       +       |  <-- center (200, 6k)
                   +---------------+
  low_anchor (100, 2k)      high_flow (250, 2k)
                   +---------------------> High Speed (Flow)
```

### Why Box-Style Factorial Sampling?
* **Minimal Test Count:** Samples the 4 corners of the practical printing envelope plus 1 mid-point check, capturing both main effects (flow dependence, acceleration dependence) and cross-term interactions with only 5 measurements.
* **Non-Linearity Verification:** Comparing the center point measurement against the bilinear interpolation of the 4 corners tests whether the pressure surface remains smooth across the envelope.

---

## Signal Quality & Confidence Scoring Algorithm

`APA_FINISH_CELL` evaluates measurement quality using a heuristic confidence score ($0$ to $100$):

$$\text{Confidence} = 30 + S_{\text{SNR}} + S_{\text{balance}} + S_{\text{clean}}$$

1. **Signal-to-Noise Ratio Points ($S_{\text{SNR}}$):**
   * Peak $\lvert\text{area}\rvert \ge 20000 \implies S_{\text{SNR}} = 40$ (Strong signal)
   * Peak $\lvert\text{area}\rvert \ge 5000 \implies S_{\text{SNR}} = 34$ (Good signal)
   * Peak $\lvert\text{area}\rvert \ge 1000 \implies S_{\text{SNR}} = 22$ (Moderate signal)
   * Peak $\lvert\text{area}\rvert \ge 300 \implies S_{\text{SNR}} = 12$ (Weak signal)
   * Peak $\lvert\text{area}\rvert < 300 \implies S_{\text{SNR}} = 5$ (Very weak signal)

2. **Bracket Balance Score ($S_{\text{balance}}$):** Evaluates how symmetrically the test steps straddle zero ($S_{\text{balance}} = \lfloor 20 \cdot \text{balance} \rfloor$).

3. **Clean Zero Bonus ($S_{\text{clean}}$):** Adds $+10$ points if $a_0 < 0.02 \cdot \text{peak}$ and $a_1 < 0.15 \cdot \text{peak}$.

4. **Grade Tiers:**
   * **80 - 100:** Excellent (Keep row)
   * **65 - 79:** Good (Keep row)
   * **45 - 64:** Fair (Usable, optional fine step re-run)
   * **30 - 44:** Weak (Consider matching neighboring corner)
   * **< 30:** Unusable (Widen search range or re-run)

---

## Kinematic Conversion Equations

Print settings (XY motion) are mapped into extruder velocity and acceleration during `FLOW_MEASURE_K` using filament and line geometry:

$$\text{Line Area: } A_{\text{line}} = h_{\text{layer}} \cdot (w_{\text{line}} - h_{\text{layer}}) + \pi \cdot \left(\frac{h_{\text{layer}}}{2}\right)^2$$

$$\text{Filament Area: } A_{\text{fil}} = \pi \cdot \left(\frac{d_{\text{fil}}}{2}\right)^2$$

$$\text{Extrusion Ratio: } R = \frac{A_{\text{line}}}{A_{\text{fil}}}$$

$$\text{Volumetric Flow: } Q = v_{\text{XY}} \cdot A_{\text{line}}$$

$$\text{Extruder Speed: } v_E = v_{\text{XY}} \cdot R$$

$$\text{Extruder Acceleration: } a_E = a_{\text{XY}} \cdot R$$

---

### Worked Example (Default U1 Geometry)

Using default printer parameters:
* **Layer Height ($h_{\text{layer}}$):** $0.2\text{ mm}$
* **Line Width ($w_{\text{line}}$):** $0.45\text{ mm}$
* **Filament Diameter ($d_{\text{fil}}$):** $1.75\text{ mm}$
* **Target XY Condition:** $100\text{ mm/s}$ at $2000\text{ mm/s}^2$ (`low_anchor`)

**Calculated Values:**

$$\text{Line Area: } A_{\text{line}} = 0.2 \cdot (0.45 - 0.2) + \pi \cdot (0.1)^2 = \mathbf{0.081416\text{ mm}^2}$$

$$\text{Filament Area: } A_{\text{fil}} = \pi \cdot (0.875)^2 = \mathbf{2.405157\text{ mm}^2}$$

$$\text{Extrusion Ratio: } R = \frac{0.081416}{2.405157} = \mathbf{0.033851}$$

$$\text{Volumetric Flow: } Q = 100 \cdot 0.081416 = \mathbf{8.14\text{ mm}^3\text{/s}}$$

$$\text{Extruder Speed: } v_E = 100 \cdot 0.033851 = \mathbf{3.39\text{ mm/s}}$$

$$\text{Extruder Acceleration: } a_E = 2000 \cdot 0.033851 \approx \mathbf{68\text{ mm/s}^2}$$

These conversions allow pure-E test steps executed at the discard position to precisely emulate nozzle pressure states experienced during actual printing at target $(v_{\text{XY}}, a_{\text{XY}})$.

---


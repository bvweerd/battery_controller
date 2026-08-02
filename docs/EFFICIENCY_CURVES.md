# Efficiency curve examples for known home batteries

This document collects **measured** part-load efficiency data for commercially available
home battery systems and turns it into ready-to-paste values for the
**Charge efficiency curve** and **Discharge efficiency curve** fields of this integration.

If your system is not listed, jump to [Deriving your own curve](#deriving-your-own-curve).

---

## 1. What the curve fields actually mean here

The optimizer treats battery power as an **AC setpoint** at the house connection point and
applies the curve to the SoC transition:

| Direction | SoC transition | Meaning of the curve value |
| --- | --- | --- |
| Charging | `soc += P_ac × Δt × charge_eff(P_ac)` | fraction of AC energy that ends up **stored** |
| Discharging | `soc -= P_ac × Δt / discharge_eff(P_ac)` | fraction of **stored** energy that reaches AC |

So each curve is a **one-way, full-path** efficiency: AC terminals ↔ cells, covering
the inverter, the DC/DC stage, the BMS and the cells themselves.
`charge_eff × discharge_eff` = round-trip efficiency at that power.

**Format**: `power_kw:efficiency` pairs separated by commas, e.g.

```
0.05:0.78, 0.1:0.84, 0.2:0.90, 0.5:0.94, 10:0.958
```

Values are linearly interpolated between points and **clamped flat outside the range** —
below the first point the first value is used, above the last point the last value.
A single plain number (`0.95`) means a flat, power-independent curve.

> ### ⚠️ The dominant effect is the *opposite* of what you might expect
>
> Cell resistive losses do grow with current, so the battery alone gets slightly worse at
> high power. But in a complete home battery *system* that effect is swamped by the
> **inverter's part-load behaviour**: a fixed standby/idle loss of roughly 30–60 W has to
> be paid out of whatever power is flowing. At 100 W output that overhead eats a third or
> more of the energy; at 5 kW it is negligible.
>
> The result is that real curves **rise steeply** from low power and then flatten —
> they do not fall. This matters, because home batteries spend much of the night
> discharging at exactly 100–300 W, which is where the curve is worst.

---

## 2. Where these numbers come from

All measured data below is from the **HTW Berlin / aquu "Stromspeicher-Inspektion 2026"**,
an annual independent lab test of home battery systems
([study page](https://solar.htw-berlin.de/studien/stromspeicher-inspektion-2026/),
[full PDF](https://solar.htw-berlin.de/wp-content/uploads/HTW-aquu-Stromspeicher-Inspektion-2026.pdf)).
It is the only public source that publishes **part-load efficiency curves for named
products** rather than a single datasheet headline number.

Three separate measurements are combined here:

| Source | What it gives |
| --- | --- |
| Figures 19 & 20 | Inverter **discharge** conversion efficiency vs. AC output power, 0–500 W, per named product |
| Figure 16 | Mean conversion efficiency per path (AC→battery, battery→AC), averaged over 5–95 % of nominal power |
| Figure 12 | **Battery-side** efficiency (cells + BMS), charge and discharge losses combined |

The composed integration curve is:

```
discharge_eff(P) = η_inverter,discharge(P) × √η_battery
charge_eff(P)    = η_inverter,charge(P)    × √η_battery
```

`√η_battery` splits the battery's own round-trip loss evenly over the two directions,
matching the convention this integration uses elsewhere.

### Accuracy caveat

Values at 100 W and the "least efficient" figures are **stated numerically in the study
text** and are exact. All other points were **read off the published charts** and are
accurate to roughly ±1–2 percentage points. That is well inside the spread between
individual units, so treat every curve below as a good starting point rather than a
calibration of *your* specific unit. The integration's own charge/discharge calibration
will refine the SoC transitions at runtime regardless.

---

## 3. Measured part-load discharge efficiency (inverter only)

Conversion efficiency in discharge mode, as a function of **AC output power**.
Battery-side losses are *not* included here — see §4.

### 5 kW class

| AC output | SAX Power Home Plus | SMA Sunny Boy Smart Energy 5.0 | KOSTAL PLENTICORE MP G3 M 4.6 |
| ---: | ---: | ---: | ---: |
| 25 W | 58 % | — | — |
| 50 W | 75 % | 61 % | 61 % |
| 75 W | 81 % | 70 % | 69 % |
| **100 W** | **84 %** | **76 %** | **75 %** |
| 150 W | 89 % | 82 % | 81 % |
| 200 W | 91 % | 86 % | 85 % |
| 250 W | 93 % | 88 % | 87 % |
| 300 W | 94 % | 90 % | 89 % |
| 400 W | 95 % | 92 % | 91 % |
| 450 W | 96 % | 94 % | 92 % |
| **reaches 90 %** | **at 167 W** | **at 280 W** | **at 351 W** |

Bold rows are values stated explicitly in the study text.
The lower bound of each curve is the inverter's **minimum AC output**: SAX Power starts at
25 W, KOSTAL at 50 W, while SMA states it can serve loads from 1 W.

### 10 kW class

| AC output | RCT POWER Power Storage DC 10.0 | ENERGY DEPOT Centurio 10 | FOX ESS PQ-H3-Ultra-10.0 | FRONIUS Symo GEN24 10.0 Plus SC | KOSTAL PLENTICORE G3 M 10 | least efficient tested |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 50 W | 80 % | 63 % | 57 % | 57 % | 49 % | — |
| 75 W | 84 % | 72 % | 67 % | 67 % | 60 % | 45 % |
| **100 W** | **86 %** | 78 % | 74 % | 74 % | 68 % | **54 %** |
| 150 W | 90 % | 83 % | 81 % | 81 % | 76 % | 64 % |
| 200 W | 92 % | 85 % | 85 % | 85 % | 81 % | 70 % |
| 250 W | 93 % | 87 % | 87 % | 87 % | 83 % | 74 % |
| 300 W | 94 % | 89 % | 89 % | 88 % | 85 % | 77 % |
| 400 W | 95 % | 90 % | 91 % | 90 % | 88 % | 82 % |
| 500 W | 96 % | 91 % | 93 % | 92 % | 91 % | 86 % |

At 100 W the spread across the 10 kW class is **54 % to 86 %** — a factor that no single
round-trip-efficiency number can express, and precisely what these curves exist to capture.

---

## 4. Battery-side and mean path efficiencies

| System | Product | Battery η | AC→battery (mean) | battery→AC (mean) |
| --- | --- | ---: | ---: | ---: |
| A1 | SAX Power Home Plus | 95.8 % | 97.4 % | 97.7 % |
| B1 | KOSTAL PLENTICORE MP G3 M 4.6 (AC) + BYD Battery-Box HVS+ 7.7 | 95.2 % | 94.8 % | 95.1 % |
| B2 | KOSTAL PLENTICORE G3 M 10 (AC) + BYD Battery-Box Premium HVS 12.8 | 96.0 % | 96.8 % | 96.9 % |
| B3 | KOSTAL PLENTICORE MP G3 M 4.6 (DC) + BYD Battery-Box HVS+ 7.7 | 95.2 % | 94.8 % | 95.1 % |
| B4 | KOSTAL PLENTICORE G3 M 10 (DC) + BYD Battery-Box Premium HVS 12.8 | 96.0 % | 96.8 % | 96.9 % |
| C1 | SMA Sunny Boy Smart Energy 5.0 + Home Storage 6.5 | 94.8 % | 96.0 % | 96.2 % |
| D1 | FRONIUS Symo GEN24 10.0 Plus SC + Reserva 12.6 | 95.1 % | 96.7 % | 97.0 % |
| E1 | FOX ESS PQ-H3-Ultra-10.0 + EQ3300-5 | 97.1 % | 97.7 % | 97.6 % |
| F1 | RCT POWER Power Storage DC 10.0 + Power Battery 11.5 | 96.0 % | 97.7 % | 97.8 % |
| G1 | ENERGY DEPOT Centurio 10 + DOMUS 2.5 | 95.7 % | 96.9 % | 96.8 % |
| H1 | (manufacturer withheld consent) | 95.2 % | — | 91.2 % |
| I1 | (manufacturer withheld consent) | 87.9 % | 95.0 % | 95.6 % |
| **Ø** | average of all 12 | **95.0 %** | **96.6 %** | **96.2 %** |

The mean path efficiencies are averaged over 5–95 % of nominal power, so they are much
higher than the 100 W part-load values above. They are the right thing to use for the
**charge** side, where there is no published part-load curve and where charging
realistically happens at PV-surplus or block-charge power levels rather than at 100 W.

---

## 5. Ready-to-paste curves

Copy these straight into the battery subentry fields. Each pair is `kW:efficiency`.
The final point is at the system's nominal power and comes from the mean path efficiency.

### RCT POWER Power Storage DC 10.0 + Power Battery 11.5
*Best part-load behaviour measured — 86 % already at 100 W.*
```
charge:    0.957
discharge: 0.05:0.784, 0.1:0.843, 0.15:0.882, 0.2:0.901, 0.25:0.911, 0.3:0.921, 0.4:0.931, 0.5:0.941, 10:0.958
```

### FOX ESS PQ-H3-Ultra-10.0 + EQ3300-5
*Overall test winner in the 10 kW class; strong at high power, average at part load.*
```
charge:    0.963
discharge: 0.05:0.562, 0.1:0.729, 0.15:0.798, 0.2:0.838, 0.25:0.857, 0.3:0.877, 0.4:0.897, 0.5:0.916, 10:0.962
```

### ENERGY DEPOT Centurio 10 + DOMUS 2.5
```
charge:    0.948
discharge: 0.05:0.616, 0.1:0.763, 0.15:0.812, 0.2:0.832, 0.25:0.851, 0.3:0.871, 0.4:0.880, 0.5:0.890, 10:0.947
```

### FRONIUS Symo GEN24 10.0 Plus SC + Reserva 12.6
```
charge:    0.943
discharge: 0.05:0.556, 0.1:0.722, 0.15:0.790, 0.2:0.829, 0.25:0.848, 0.3:0.858, 0.4:0.878, 0.5:0.897, 10:0.946
```

### KOSTAL PLENTICORE G3 M 10 + BYD Battery-Box Premium HVS 12.8
```
charge:    0.948
discharge: 0.05:0.480, 0.1:0.666, 0.15:0.745, 0.2:0.794, 0.25:0.813, 0.3:0.833, 0.4:0.862, 0.5:0.892, 10:0.949
```

### SAX Power Home Plus
*Best part-load behaviour in the 5 kW class (multi-level topology).*
```
charge:    0.953
discharge: 0.025:0.568, 0.05:0.734, 0.075:0.793, 0.1:0.822, 0.15:0.871, 0.2:0.891, 0.25:0.910, 0.3:0.920, 0.4:0.930, 0.45:0.940, 4.6:0.956
```

### SMA Sunny Boy Smart Energy 5.0 + Home Storage 6.5
```
charge:    0.935
discharge: 0.05:0.594, 0.1:0.740, 0.15:0.798, 0.2:0.837, 0.25:0.857, 0.3:0.876, 0.4:0.896, 0.45:0.915, 5:0.937
```

### KOSTAL PLENTICORE MP G3 M 4.6 + BYD Battery-Box HVS+ 7.7
```
charge:    0.925
discharge: 0.05:0.595, 0.1:0.732, 0.15:0.790, 0.2:0.829, 0.25:0.849, 0.3:0.868, 0.4:0.888, 0.45:0.898, 4.6:0.928
```

### Generic fallbacks

For a system that is not listed, pick the class that matches your hardware:

| Profile | charge | discharge |
| --- | --- | --- |
| Modern hybrid inverter, good part load | `0.95` | `0.05:0.75, 0.1:0.84, 0.2:0.90, 0.5:0.93, 10:0.95` |
| Typical hybrid inverter (use if unsure) | `0.94` | `0.05:0.58, 0.1:0.73, 0.2:0.84, 0.5:0.91, 10:0.95` |
| AC-coupled retrofit / older inverter | `0.92` | `0.05:0.45, 0.1:0.55, 0.2:0.70, 0.5:0.86, 10:0.93` |
| Flat curve (pre-curve behaviour, RTE 0.90) | `0.9487` | `0.9487` |

---

## 6. Deriving your own curve

### From an idle-loss figure

An inverter's efficiency curve is captured well by a three-term loss model:

```
P_loss(P) = P_idle + a·P + b·P²
η(P)      = P / (P + P_loss(P))
```

`P_idle` dominates at low power and is the number you actually need. The study measured
a 10 kW hybrid inverter (KOSTAL PLENTICORE G3 M 10) at **≈50 W idle loss** and **228 W
total loss at 10 kW output**, i.e. 97.8 % at nominal power. If your inverter's datasheet
quotes a standby or self-consumption figure, plug it in as `P_idle`, use your datasheet's
peak or EU efficiency to pin the top of the curve, and sample at 50 / 100 / 200 / 500 W
and nominal power.

### By measurement

More reliable than any table. With the battery in a steady discharge at a fixed setpoint:

1. Set a constant discharge power (e.g. 200 W) and let it run for at least 30 minutes.
2. Record the **AC energy delivered** and the **SoC drop × usable capacity** over that window.
3. `discharge_eff = AC energy out / battery energy drawn`.
4. Repeat at 100 W, 500 W, 1 kW and near nominal power.

Do the same for charging, comparing AC energy in against stored energy. Avoid windows in
which the BMS derates near the SoC extremes — the numbers there reflect throttling, not
efficiency.

### Sanity checks

- Efficiency must be in `(0, 1]`; the config flow rejects anything outside that range.
- The curve should be **monotonically rising** over the part-load region. A curve that
  falls from its zero-power value is almost certainly wrong for a complete system.
- `charge_eff × discharge_eff` at nominal power should land around **0.86–0.93**.
  If your composed curve implies a round-trip efficiency above 0.95, you have most likely
  omitted either the battery-side losses or the inverter's part-load penalty.

---

## Sources

- [Stromspeicher-Inspektion 2026 — HTW Berlin](https://solar.htw-berlin.de/studien/stromspeicher-inspektion-2026/)
- [Stromspeicher-Inspektion 2026 (full PDF)](https://solar.htw-berlin.de/wp-content/uploads/HTW-aquu-Stromspeicher-Inspektion-2026.pdf)
- [Stromspeicher-Inspektion overview — HTW Berlin](https://solar.htw-berlin.de/themen/stromspeicher-inspektion/)
- [Battery Storage Inspection 2026 — heise online](https://www.heise.de/en/news/Battery-Storage-Inspection-2026-Large-differences-in-efficiency-and-warranty-11200179.html)

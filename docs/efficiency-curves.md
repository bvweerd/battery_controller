# Efficiency curve examples for known home batteries

This document collects part-load efficiency data for commercially available home battery
systems and turns it into ready-to-paste values for the **Charge efficiency curve** and
**Discharge efficiency curve** fields of this integration.

- **§3–§5** cover installed hybrid systems (KOSTAL, FRONIUS, SMA, FOX ESS, RCT, SAX,
  ENERGY DEPOT, BYD), based on **published lab measurements** of the curve itself.
- **§6** covers the plug-in batteries common on the Dutch market (Marstek, Zendure,
  HomeWizard). No lab publishes curves for these, but **owners measure them**: the
  Marstek Venus A and E curves come from community measurements, and the other two borrow its
  shape anchored on their own measured round-trip efficiency.

Each curve says where it came from. Lab-measured, user-measured and modelled are not the
same thing, and the difference is large enough to matter.

If your system is not listed, jump to [§7 Deriving your own curve](#7-deriving-your-own-curve).

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
> **inverter's operating overhead**: a fixed 40–60 W that has to be paid out of whatever
> power is flowing, whether that is 100 W or 5 kW. At 100 W it eats a third to half of the
> energy; at 5 kW it is a rounding error.
>
> The result is that real curves **rise steeply** from low power and then flatten. They do
> not fall — except mildly near rated power, where the resistive term finally catches up.
>
> This matters because home batteries spend much of the night discharging at exactly
> 100–300 W, which is where the curve is worst. Measured examples: a 10 kW hybrid delivers
> 54–86 % at 100 W depending on make (§3), and a plug-in unit held at 100 W throws away
> more than half the energy round-trip (§6).

---

## 2. Where these numbers come from

Three classes of source appear in this document, in descending order of confidence:

| Class | Used for | Confidence |
| --- | --- | --- |
| **Lab-measured curve** | §3–§5, installed hybrids | High — the curve itself was measured |
| **User-measured curve** | §6, Marstek Venus A and E | Good — several power points, but one owner's unit |
| **RTE-anchored model** | §6, Zendure and HomeWizard | Indicative — endpoint measured, shape borrowed |

The lab data below is from the **HTW Berlin / aquu "Stromspeicher-Inspektion 2026"**,
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

## 6. Dutch-market plug-in batteries (Marstek, Zendure, HomeWizard)

The HTW study covers permanently installed hybrid systems. The plug-in ("stekker")
batteries that dominate the Dutch market are a different class. No lab publishes
part-load curves for them — but **owners measure them**, and for the Marstek Venus that
community data is detailed enough to be a curve in its own right.

### The Marstek Venus E/C — user-measured charge curve

Measured by users on the German [Photovoltaikforum](https://www.photovoltaikforum.com/thread/241171-marstek-venus-c-e-ac-speicher-5-12-kwh-erfahrungen-installation-leistung-im-allt/),
charging efficiency against AC input power:

| AC input | 100 W | 500 W | ~1500 W | 2000–2200 W |
| --- | ---: | ---: | ---: | ---: |
| Charge efficiency | **64 %** | **90 %** | **93 %** (peak) | **92 %** |

Those four points pin the shape exactly, and the shape is not what a datasheet suggests:

```
loss(P) = 55 W + 2.7e-5 · P²      →  fits all four points to within 1 pp
```

The loss is **flat at ~55 W from 100 W to 500 W** — it barely moves — and only then does
the quadratic term take over and pull efficiency back down above ~1500 W.

> ### ⚠️ Standby draw is not operating overhead
>
> The Marstek's *idle* draw is about 7 W. Its *operating* overhead is **55 W** — eight
> times higher. The control electronics, switching stage and BMS all wake up the moment
> the unit converts anything, and that cost is paid at 100 W just as much as at 2500 W.
>
> This matters because deriving a curve from the datasheet standby figure — the obvious
> thing to do — produces a curve that is far too optimistic at low power. For the Marstek
> a standby-based model predicts 86 % at 100 W; users measure **64 %**. Anchor on
> operating overhead, not standby.

### The Marstek Venus A — measured at two discharge powers

The Venus A (2120 Wh nominal, **1500 W bidirectional** inverter, 2.4 kW of PV input) was
tested by
[smartzone.de](https://www.smartzone.de/marstek-venus-a-im-test-das-budget-modell-mit-grossartiger-leistung/)
in a way that isolates the curve, because they ran the same full cycle at two discharge
powers:

| Measurement | Value |
| --- | ---: |
| Grid energy for a full charge | 2230 Wh |
| Discharged at **200 W** | 1750 Wh → RTE **78.5 %** |
| Discharged at **100 W** | 1550 Wh → RTE **69.5 %** |
| Usable capacity (measured) | 1750 Wh of 2120 Wh nominal (83 %) |
| Standby draw | ~0.1 W from the mains |

Halving the discharge power costs **9 percentage points of round-trip efficiency**.

Those points only constrain the curve up to 200 W, though. The high-power end comes from
an owner running **Venus A units at 1200 W**, who reports **79–80 %** round-trip from the
batteries' own energy counters over roughly **300 kWh** of throughput. Fitting both
sources together:

```
loss(P) = 30 W + 7.95e-5 · P²
```

| Constraint | Source | Target | Fit |
| --- | --- | ---: | ---: |
| discharge 100 W vs 200 W | smartzone | 0.886 | 0.891 |
| round trip at 1200 W | owner, ~300 kWh | 0.795 | 0.797 |
| full cycle at 200 W discharge | smartzone | 0.785 | 0.781 |

All three land within half a percentage point, and the fit implies smartzone charged at
roughly 600 W — plausible, and a figure they never stated.

The owner's counters were checked with the idle test below and **do not tick while the
unit sits idle**, so that 79–80 % is conversion efficiency rather than a whole-system
figure — which is what makes it usable as a curve anchor. Their reported capacity,
cycle count and throughput are mutually consistent (4.24 kWh nominal, ~0.75 cycles/day
over 90 days ≈ 300 kWh).

> **The Venus A peaks around 500–800 W, not at its rated power.**
>
> | Power | Round trip | Full discharge takes (3.52 kWh) |
> | ---: | ---: | ---: |
> | 300 W | 79 % | 11.7 h |
> | **500 W** | **83 %** | 7.0 h |
> | **800 W** | **83 %** | 4.4 h |
> | 1200 W | 80 % | 2.9 h |
> | 1500 W | 77 % | 2.3 h |
>
> The plateau is wide and flat: 500 W and 800 W are worth the same, so **800 W is the
> better default** — identical efficiency in half the time. Above that the quadratic term
> takes over, and running at the full 1500 W costs **3 points one-way**, 5 points round
> trip, against 800 W.
>
> This is exactly the trade-off the DP exploits once the curve is loaded, and it is not
> a free choice: a slower discharge needs a longer window. Overnight there is usually
> room, but a cheap-price charging window of three hours will not fit a 500 W charge —
> which is why the optimizer, not a fixed setpoint, should be making the call.

Note the curve mixes variants: the low-power points come from a 2.12 kWh unit and the
1200 W point from 4.24 kWh units. The inverter is the same 1500 W bidirectional stage in
both; only the pack differs, so the larger unit runs at a lower C-rate and should if
anything be marginally better.

> **The Venus A beats the Venus E where it counts.** Its operating overhead is **30 W
> against the Venus E's 55 W**, so through the whole part-load region it is the more
> efficient unit — 58 % round-trip at 100 W where the Venus E manages 41 %. Only near
> rated power does the Venus E pull ahead, because its 2500 W stage is not yet fighting
> its own resistive losses where the Venus A's 1500 W stage already is.
>
> The headline numbers say the opposite (78.5 % for the A, 82–83 % for the E) purely
> because they were measured at different powers: the A at a realistic 200 W, the E near
> full power. **Two RTE figures from two reviews are not comparable unless you know the
> power each was taken at.** This is the single most useful thing the curve model buys you.

It also settles the standby question. The Venus A draws **0.1 W** standby against the
Venus E's 7 W — seventy times less — yet its operating overhead is only 1.8× lower
(30 W vs 55 W). Standby tells you almost nothing about the curve.

### Curves

| System | Provenance |
| --- | --- |
| Marstek Venus A | **User-measured**, two independent owners: a wall-meter test at 100/200 W plus ~300 kWh of counter data at 1200 W — the best-constrained curve here |
| Marstek Venus E | **User-measured** charge curve; discharge scaled to the measured full-power RTE |
| Zendure, HomeWizard | RTE anchor is measured; the *shape* is borrowed from the Marstek fit |

**Marstek Venus A** — 1500 W bidirectional, overhead 30 W, plateau 500–800 W
```
charge:    0.05:0.623, 0.1:0.764, 0.2:0.857, 0.3:0.890, 0.5:0.909, 0.8:0.908, 1.2:0.892, 1.5:0.878
discharge: 0.05:0.623, 0.1:0.764, 0.2:0.857, 0.3:0.890, 0.5:0.909, 0.8:0.908, 1.2:0.892, 1.5:0.878
```
Set both power limits to 1.5 kW. The derived `round_trip_efficiency` the integration
reports from this curve is 0.765 — the mean over 5–95 % of rated power, which sits below
the 0.83 plateau because it includes the poor bottom end.
The two directions share one loss function: a full-cycle RTE plus a discharge-power ratio
constrains the shape but cannot separate charge from discharge.

> **Watch the review figures on this unit.** smartzone lists an 1800 W maximum charge rate;
> Marstek's own specification is a **1500 W bidirectional** inverter, and owners confirm
> 1500 W. Other sources quote 1200 W continuous / 1440 W peak, but those are the *backup*
> (off-grid) ratings, which are a different thing from the grid-tied limit. Only the
> endpoint of the curve depends on this — the fit is anchored at 100, 200 and 1200 W.

**Marstek Venus E 3.0 / 4.0** — 2500 W, overhead 55 W, measured RTE 82–83 %
```
charge:    0.05:0.476, 0.1:0.644, 0.2:0.781, 0.3:0.839, 0.5:0.890, 0.8:0.917, 1.2:0.927, 2:0.925, 2.5:0.918
discharge: 0.05:0.466, 0.1:0.631, 0.2:0.765, 0.3:0.822, 0.5:0.872, 0.8:0.898, 1.2:0.908, 2:0.905, 2.5:0.899
```

**Zendure SolarFlow 2400 PRO / AC+** — 2400 W, overhead ~42 W, measured RTE 87–88 % at 1200 W
```
charge:    0.05:0.541, 0.1:0.701, 0.2:0.821, 0.3:0.870, 0.5:0.910, 0.8:0.930, 1.2:0.935, 2.4:0.922
discharge: 0.05:0.541, 0.1:0.701, 0.2:0.821, 0.3:0.870, 0.5:0.910, 0.8:0.930, 1.2:0.935, 2.4:0.922
```

**HomeWizard Plug-In Battery** — 800 W, overhead ~50 W, measured RTE 78.4 % at 800 W
```
charge:    0.05:0.501, 0.1:0.665, 0.2:0.791, 0.3:0.840, 0.5:0.876, 0.8:0.885
discharge: 0.05:0.501, 0.1:0.665, 0.2:0.791, 0.3:0.840, 0.5:0.876, 0.8:0.885
```

### Round-trip efficiency actually delivered

This is the number that decides whether a trade is profitable:

| AC power | Marstek Venus A | Marstek Venus E | Zendure 2400 | HomeWizard |
| ---: | ---: | ---: | ---: | ---: |
| 100 W | 58 % | **41 %** | 49 % | 44 % |
| 300 W | 79 % | 69 % | 76 % | 71 % |
| 500 W | **83 %** (peak) | 78 % | 83 % | 77 % |
| 800 W | 82 % | 84 % | 87 % | 78 % |
| rated | 80 % | 83 % | 85 % | 78 % |

Read down the 100 W column, not the "rated" row. That column is where these units spend
most of their life, and it is where they differ most.

### Where you measure changes what you get

Efficiency figures for the same unit disagree by 5–10 points depending on the instrument.
Before comparing any two numbers, establish which of these produced them:

| Instrument | What it captures | Watch out for |
| --- | --- | --- |
| **Wall meter** (plug-in energy meter, P1) | Everything: conversion, standby, idle drain | Standby is counted even when the battery does nothing |
| **Battery's own counters** (app, HA sensors) | Usually AC-side energy while actively converting | Unknown measurement plane; standby may or may not be counted; internal counters are less trustworthy than a meter |
| **Single back-to-back cycle** | Close to pure conversion at one power | Only valid for the power you ran it at |

The Venus A curve above is fitted to the first and second of these, from two different
owners, and they agree to within half a percentage point once the power dependence is
accounted for. That agreement is the reason to trust the fit.

> ### ⚠️ Standby does not belong in the curve
>
> Whatever your metered figure, do not raise the curve's overhead term until it reproduces
> it. Three reasons:
>
> 1. **The shape will not accept it.** Forcing the Venus E to 79.5 % at 1200 W by raising
>    the fixed overhead gives 30 % round-trip at 100 W — but 69.5 % was *measured* there.
>    If a gap does not fit the shape, it is not a conversion loss.
> 2. **Standby is a sunk cost, not a marginal one.** The unit draws it whether or not it
>    charges. Marginal cost is what decides a trade, so folding standby in makes the
>    optimizer refuse arbitrage that is genuinely profitable.
> 3. **Runtime calibration already handles the drift.** The integration compares planned
>    against actual SoC each step and corrects the *transitions* — never the costs. A slow
>    standby drain shows up there and is absorbed automatically.
>
> A metered whole-system figure is the right number for "is this battery worth owning".
> It is the wrong number for "should the optimizer take this trade".

### Settling it on your own unit

If you are working from the battery's own counters, the first thing to establish is
whether they tick while the unit is doing nothing. That single fact decides whether your
figure is conversion efficiency or whole-system efficiency, and the two can differ by six
points or more.

**The idle test — two hours, no effort.** Park the battery in idle (no charge, no
discharge, no PV) and read the "charged" counter before and after.

| Counter after idling | Verdict | What to do |
| --- | --- | --- |
| Unchanged | Standby is **excluded**; your figure is conversion efficiency | Use it to anchor the curve directly |
| Grew | Standby is **included**; your figure is whole-system efficiency | Subtract it before anchoring — see below |

Idle overnight rather than for two hours if your counter reads in whole kWh or two
decimals: 6 W for two hours is only 0.012 kWh, which can hide under the display
resolution and read as a false "unchanged". Ten hours puts it at 0.06 kWh.

If it grew, the increase over two hours gives you the standby power directly. Combine it
with the period your throughput accumulated over:

```
conversion_RTE = metered_RTE × energy_in / (energy_in − standby_W × 24 × days / 1000)
```

The correction is not small. At 300 kWh throughput, 6 W of standby moves the answer from
82 % over two months to 87 % over six — so the elapsed time matters as much as the
standby figure.

**For the shape rather than the endpoint**, run a full charge followed immediately by a
full discharge at a fixed power, and repeat at a second power. Two points at different
powers is all it takes; that is exactly what pins the Venus A curve above.

### Cross-check against owner reports

HomeWizard owners running Nul-op-de-Meter — which holds the battery at household baseload
for hours — report round-trip efficiency sagging to **~74 %** against the 78.4 % measured
at its rated 800 W. The table above puts 74 % at roughly 400 W, which is exactly a typical
Dutch household baseload. Independent measurement, independent model, same answer.

### What this means for scheduling

**A plug-in battery held at 100 W throws away more than half the energy.** That is not a
rounding error the optimizer can absorb — at 41 % round trip, arbitrage needs a spread of
well over 3× the naive estimate before it breaks even, and zero-grid tracking of a small
baseload is close to pointless.

Two practical consequences:

- **Enter the real curve.** A Marstek entered as a flat `0.9487` (RTE 0.90) looks 7
  percentage points better than it is at rated power and **more than twice as good** as it
  is at 100 W. The optimizer will take trades that lose money.
- **The curve makes the optimizer prefer bursts.** With a real curve loaded, discharging
  1 kWh as 500 W for two hours beats trickling it out at 100 W for ten — and the DP will
  now find that on its own, because that is precisely what a power-dependent efficiency
  model is for.

---

## 7. Deriving your own curve

### From an overhead figure

An inverter's efficiency curve is captured well by a two-term loss model:

```
P_loss(P) = P_overhead + b·P²
η(P)      = P / (P + P_loss(P))
```

`P_overhead` dominates at low power and is the number you actually need.

> ⚠️ **Do not use the datasheet standby figure for `P_overhead`.** Standby is what the
> unit draws while *sleeping*; overhead is what it burns while *converting*, and the two
> differ by close to an order of magnitude. The Marstek Venus specifies ~7 W standby but
> its measured operating overhead is ~55 W (§6). Using the standby number produces a
> curve that is wildly optimistic in exactly the region where it matters most.

Get `P_overhead` by rearranging a measurement instead. From one round-trip efficiency
measured at a known power `P_ref`:

```
loss(P_ref) = P_ref / √RTE − P_ref
P_overhead  = loss(P_ref) − b·P_ref²
```

For `b`, the quadratic loss at rated power is around **6–7 % of the rating** for the
plug-in class (2.7e-5 × 2500² = 169 W for the Marstek). Installed hybrids are far better:
the HTW study measured the KOSTAL PLENTICORE G3 M 10 at **≈50 W idle** and **228 W total
loss at 10 kW**, i.e. 97.8 % at nominal power. Two anchor points are always better than
one — measure at low and high power if you can.

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
- `charge_eff × discharge_eff` at nominal power should land around **0.86–0.93** for an
  installed hybrid, or **0.78–0.88** for a plug-in unit. If your composed curve implies a
  round-trip efficiency above 0.95, you have most likely omitted either the battery-side
  losses or the inverter's part-load penalty.
- At 100 W the round trip should be *bad* — roughly 0.5–0.75 for a hybrid and 0.4–0.5 for
  a plug-in unit. A curve that still shows 0.85 at 100 W is almost certainly built on a
  standby figure rather than a real operating overhead.

---

## Sources

**Installed hybrid systems (§3–§5)**

- [Stromspeicher-Inspektion 2026 — HTW Berlin](https://solar.htw-berlin.de/studien/stromspeicher-inspektion-2026/)
- [Stromspeicher-Inspektion 2026 (full PDF)](https://solar.htw-berlin.de/wp-content/uploads/HTW-aquu-Stromspeicher-Inspektion-2026.pdf)
- [Stromspeicher-Inspektion overview — HTW Berlin](https://solar.htw-berlin.de/themen/stromspeicher-inspektion/)
- [Battery Storage Inspection 2026 — heise online](https://www.heise.de/en/news/Battery-Storage-Inspection-2026-Large-differences-in-efficiency-and-warranty-11200179.html)

**Dutch-market plug-in batteries (§6)**

- [HomeWizard Plug-in Battery review — energienerds.nl](https://energienerds.nl/index.php/2026/03/26/homewizard-plug-in-battery-review) (RTE 78.4 % over four cycles at 800 W; idle draw)
- [Waarom RTE niet hetzelfde is als efficiency — HomeWizard](https://www.homewizard.com/nl/blog/rte-efficiency-thuisbatterij/) (manufacturer's own 70–80 % real-world range)
- [Marstek Venus E 3.0 review — energienerds.nl](https://energienerds.nl/index.php/2025/11/06/review-stekkerbatterij-marstek-venus-e-3-0-ac-thuisbatterij)
- [Marstek Venus E — jeroen.nl](https://jeroen.nl/energie/opslaan/thuisbatterij/stekkerbatterij/marstek-venus-e) (standby draw ~7 W)
- [Zendure SolarFlow 2400 PRO & AC+ technical deep-dive — Jay's Desk](https://www.jaysdesk.com/en/energie/zendure-2400-pro-ac-plus-review) (RTE ~87–88 %, ~93–94 % per direction, standby < 5 W)

**User measurements (§6)**

- [MARSTEK VENUS C/E AC-Speicher — Photovoltaikforum](https://www.photovoltaikforum.com/thread/241171-marstek-venus-c-e-ac-speicher-5-12-kwh-erfahrungen-installation-leistung-im-allt/) — the Venus E charge-efficiency points (64 % @ 100 W, 90 % @ 500 W, 93 % @ ~1500 W, 92 % @ 2000–2200 W) that §6 is fitted to
- [Marstek Venus A im Test — smartzone.de](https://www.smartzone.de/marstek-venus-a-im-test-das-budget-modell-mit-grossartiger-leistung/) — the Venus A full cycle at two discharge powers (2230 Wh in; 1750 Wh out at 200 W, 1550 Wh out at 100 W) plus the 0.1 W standby figure
- [Marstek Venus E 3.0 Roundtrip Effizienz gemessen — Photovoltaikforum](https://www.photovoltaikforum.com/thread/258512-marstek-venus-e-3-0-roundtrip-effizienz-gemessen/)
- [Wirkungsgrad nur 74 % — Photovoltaikforum](https://www.photovoltaikforum.com/thread/261419-wirkungsgrad-nur-74/)
- [Marstek VENUS-E 5.12 kWh Erfahrungen — Akkudoktor Forum](https://akkudoktor.net/t/marstek-venus-e-5-12-kwh-erfahrungen/27846)

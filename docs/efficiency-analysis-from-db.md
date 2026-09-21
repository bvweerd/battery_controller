# Verifying the efficiency curve from HA recorder data

How to use SQLite queries and the stored calibration files to compare actual
battery efficiency against the configured values.

---

## 1. What you want to know

The battery controller uses an efficiency curve (RTE, e.g. 91 %) as input for
the DP. If the actual battery performs better or worse than that curve, you
either miss opportunities or plan too optimistically.

There are **two metrics** you can check:

| Metric | What it measures | Data source |
|---|---|---|
| **Dispatch fidelity** | Actually delivered / commanded (AC side) | `marstek_total_energy_discharged` / setpoint |
| **Efficiency correction** | SoC change / planned SoC change | `.storage/battery_controller_*_eff` |

They measure different things. Dispatch fidelity is on the AC side of the
inverter; the efficiency correction measures across the full conversion
(including internal battery losses).

---

## 2. Dispatch fidelity via hourly averages

```sql
-- File: home-assistant_v2.db
-- Returns: per hour, per SoC band, the ratio actual/commanded energy

WITH setpoint AS (
    SELECT start_ts, mean as setpoint_w
    FROM statistics
    WHERE metadata_id = (
        SELECT id FROM statistics_meta
        WHERE statistic_id = 'sensor.marstek_battery_setpoint_marstek'
    )
    AND start_ts >= strftime('%s', '2026-08-01')
),
energy_dchg AS (
    SELECT start_ts,
           sum - LAG(sum) OVER (ORDER BY start_ts) as delta_dchg_kwh
    FROM statistics
    WHERE metadata_id = (
        SELECT id FROM statistics_meta
        WHERE statistic_id = 'sensor.marstek_total_energy_discharged'
    )
    AND start_ts >= strftime('%s', '2026-08-01')
),
energy_chg AS (
    SELECT start_ts,
           sum - LAG(sum) OVER (ORDER BY start_ts) as delta_chg_kwh
    FROM statistics
    WHERE metadata_id = (
        SELECT id FROM statistics_meta
        WHERE statistic_id = 'sensor.marstek_total_energy_charged'
    )
    AND start_ts >= strftime('%s', '2026-08-01')
),
soc_data AS (
    SELECT start_ts, mean as soc_pct
    FROM statistics
    WHERE metadata_id = (
        SELECT id FROM statistics_meta
        WHERE statistic_id = 'sensor.marstek_battery_soc'
    )
    AND start_ts >= strftime('%s', '2026-08-01')
),
combined AS (
    SELECT
        sp.setpoint_w,
        so.soc_pct,
        ed.delta_dchg_kwh,
        ec.delta_chg_kwh,
        ABS(sp.setpoint_w) / 1000.0 as commanded_kwh
    FROM setpoint sp
    LEFT JOIN soc_data so ON so.start_ts = sp.start_ts
    LEFT JOIN energy_dchg ed ON ed.start_ts = sp.start_ts
    LEFT JOIN energy_chg ec ON ec.start_ts = sp.start_ts
    WHERE ABS(sp.setpoint_w) > 300
    AND so.soc_pct IS NOT NULL
)
SELECT
    CASE WHEN setpoint_w > 0 THEN 'discharge' ELSE 'charge' END as direction,
    CAST(soc_pct / 10 AS INT) * 10 as soc_band,
    COUNT(*) as n,
    ROUND(AVG(
        CASE WHEN setpoint_w > 0 THEN delta_dchg_kwh / commanded_kwh
             ELSE delta_chg_kwh / commanded_kwh END
    ), 4) as avg_fidelity
FROM combined
WHERE
    (setpoint_w > 0 AND delta_dchg_kwh > 0.01)
    OR (setpoint_w < 0 AND delta_chg_kwh > 0.01)
GROUP BY direction, soc_band
ORDER BY direction, soc_band;
```

### Results (September 2026, Marstek 1)

| Direction | SoC band | Average fidelity |
|---|---|---|
| discharge | 10–90 % | ~0.91–0.94 (flat, no SoC dependency) |
| charge | 10–80 % | ~0.99 |
| charge | 90 %+ | ~0.89 (CV-phase taper near full) |

**Dispatch conclusion**: battery delivers ~93.7 % of the commanded discharge
power; charging is nearly lossless up to ~80 % SoC, then taper-effect kicks in.

---

## 3. SoC-based efficiency correction (stored calibration)

The coordinator stores one file per battery per direction:

```
/media/data/homeassistant/config/.storage/
  battery_controller_<entry>_<battery>_charge_eff
  battery_controller_<entry>_<battery>_discharge_eff
```

```python
import json, statistics as stats

files = {
    'marstek_charge': '...charge_eff',
    'marstek_discharge': '...discharge_eff',
}
for label, fname in files.items():
    with open(fname) as f:
        d = json.load(f)
    data = d['data']
    samples = data.get('samples', [])
    corr = data.get('correction')
    schema = data.get('schema', 'raw_ratio')
    print(f'{label}: correction={corr:.4f}, n={len(samples)}, schema={schema}')
    if samples:
        print(f'  mean={stats.mean(samples):.4f}, '
              f'min={min(samples):.3f}, max={max(samples):.3f}')
```

### Schema interpretation

| Schema in file | Interpretation |
|---|---|
| `"schema": "efficiency_factor"` | Samples are already efficiency factors (< 1.0 = worse than configured) |
| No schema key (`"raw_ratio"`) | Old format: samples are `measured/planned` ratios |

For **discharge** in old format: `efficiency_factor = 1 / raw_ratio`
(when discharging, less SoC drop per commanded kWh means better efficiency).

### Results (September 2026)

| Battery | Direction | Raw ratio | Efficiency factor | Applied |
|---|---|---|---|---|
| Marstek 1 | charge | 1.050 (capped) | 1.050 | False |
| Marstek 1 | discharge | 0.796 | **1.256** | False |
| Marstek 2 | charge | 1.048 | 1.048 | False |
| Marstek 2 | discharge | 0.920 | **1.087** | False |

`applied: False` because efficiency_factor ≥ `CALIBRATION_APPLY_THRESHOLD`
(0.995): the battery performs at or above the configured curve, so the
correction is not applied — the optimizer is never made more optimistic than
the values the user entered.

**Efficiency conclusion**: the configured curve is conservative. Both batteries
store more energy per commanded kWh and lose less SoC per commanded discharge
kWh than planned. The corrections are capped at 105 % (CALIBRATION_APPLY_MAX)
and not passed to the DP.

---

## 4. What to do with this

| Finding | Meaning | Action |
|---|---|---|
| Dispatch fidelity 94 % | Battery delivers slightly less than commanded | Normal for Marstek; confirmed by `dispatch_fidelity` sensor |
| Efficiency factor > 1 | Battery outperforms configured curve | No automatic correction (by design) — consider raising the curve manually |
| Charge fidelity 89 % at SoC > 90 % | CV-phase taper near full charge | Normal LFP behaviour; calibration skips samples that cross the high-SoC derate threshold |

To raise the curve: multiply every point in the configured curve by 1.05
(the measured but capped correction factor). See the updated curves in
`efficiency-curves.md` §6.

---

## 5. Limitations

- **Hourly setpoint averages**: the hourly mean setpoint is not the same as
  a constant setpoint for the whole hour (BC switches every 15 min). Errors
  of ±5 % are normal.
- **SoC resolution**: 1 % step ≈ 0.05 kWh on a 5 kWh battery. Short steps
  are unreliable; the calibration logic filters them via
  `CALIBRATION_SOC_QUANTUM_FACTOR = 4.0`.
- **`statistics_short_term`**: only kept for ~10 days; use the `statistics`
  table (hourly averages) for longer periods.
- **Energy counter gaps**: the `states` table can have gaps for
  `total_energy_charged/discharged`; the `statistics` table (column `sum`)
  is more reliable for cumulative sensors.
- **Old `raw_ratio` schema**: storage files written before the
  `STORED_EFFICIENCY_FACTOR` marker was introduced need the inversion
  described above. Check `data.get('schema')` before interpreting samples.

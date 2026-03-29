# Enhancement: Solar-Only Charging Mode

## Overview

Add a new control mode (`solar_schedule`) where the battery follows the
price-optimized DP schedule for discharging, but charging is restricted to
solar surplus only — never drawing from the grid to charge. Since actual solar
availability is uncertain at planning time, the implementation combines three
complementary mechanisms:

1. **Per-timestep certainty-adjusted surplus constraint** in the optimizer
2. **Rolling re-optimization** (already present, provides MPC-style adaptation)
3. **Real-time clipping** in the controller to enforce the hard solar-only constraint

---

## Motivation

Some users want to:
- Discharge at high prices (price-optimized, as today)
- Only charge when solar surplus is available — never buy grid electricity to charge
- Avoid planning a charging schedule that assumes more solar than will actually arrive

The current `follow_schedule` mode executes whatever the DP planned, including
grid-funded charging. The new mode must respect the physical constraint that
charging power ≤ real-time solar surplus, while keeping discharge behaviour
price-optimised.

---

## Architecture

### Three-layer design

```
┌─────────────────────────────────────────────────────────┐
│  Layer 1: Optimizer (planning, 15-min cycle)            │
│  - max_charge_w[t] = surplus_forecast[t] × certainty[t] │
│  - charging cost = degradation only (no grid cost)      │
└───────────────────────┬─────────────────────────────────┘
                        │ schedule (intent)
┌───────────────────────▼─────────────────────────────────┐
│  Layer 2: Rolling re-optimization (already present)     │
│  - Runs every 15 min with actual measured SoC           │
│  - Absorbs forecast errors; re-plans from current state │
└───────────────────────┬─────────────────────────────────┘
                        │ adjusted schedule
┌───────────────────────▼─────────────────────────────────┐
│  Layer 3: Real-time controller (hard constraint, ~5s)   │
│  - actual_charge = min(scheduled_charge, solar_surplus) │
│  - discharge: follow schedule unchanged                 │
└─────────────────────────────────────────────────────────┘
```

Layer 1 makes the plan realistic. Layer 2 adapts to actual SoC deviations.
Layer 3 enforces the hard "no grid charging" constraint regardless of forecast
accuracy.

---

## Implementation Plan

### Step 1 — Certainty factor calculation (`forecast_models.py`)

Add a helper that maps cloud cover to a per-timestep certainty factor.
Uncertainty peaks at ~50% cloud cover (passing clouds cause rapid
production swings); clear and fully overcast skies are both more predictable.

```python
def solar_certainty_factor(cloud_cover_fraction: float) -> float:
    """
    Returns a derating factor [CERTAINTY_MIN, CERTAINTY_MAX] that reflects
    how predictable solar production is given current cloud cover.

    cloud_cover_fraction: 0.0 (clear) → 1.0 (fully overcast)

    Uncertainty peaks at ~0.5 (scattered clouds → rapid swings).
    Clear sky and uniform overcast are both relatively predictable.
    """
    CERTAINTY_MIN = 0.60  # worst case: scattered cloud, high variability
    CERTAINTY_MAX = 0.92  # best case: clear or uniformly overcast

    # variability in [0, 1], peaks at cloud_cover = 0.5
    variability = 1.0 - abs(cloud_cover_fraction - 0.5) * 2.0

    return CERTAINTY_MAX - variability * (CERTAINTY_MAX - CERTAINTY_MIN)
```

Input data (`cloud_cover`) is already fetched by `WeatherDataCoordinator`
from open-meteo and available per timestep.

**Uncertainty shape rationale:**

| Cloud cover | Certainty | Reason |
|---|---|---|
| 0–20% | ~0.90 | Direct radiation, highly predictable |
| 30–70% | ~0.60–0.70 | Passing clouds, rapid swings |
| 80–100% | ~0.80 | Diffuse radiation, stable but low |

---

### Step 2 — Optimizer: per-timestep charge constraint (`optimizer.py`)

Add `solar_only_charging: bool` and `solar_surplus_forecast_w: list[float]`
to the optimizer input (alongside existing `pv_forecast` and
`consumption_forecast`).

When `solar_only_charging` is True:

**Action generation** — clip max charge power per timestep:
```python
if solar_only_charging:
    available_charge_w = solar_surplus_forecast_w[t]  # already certainty-adjusted
    step_max_charge_w = min(battery_max_charge_w, available_charge_w)
else:
    step_max_charge_w = battery_max_charge_w

actions = range(0, step_max_charge_w + POWER_STEP_W, POWER_STEP_W)  # charge
```

**Cost function** — charging from solar has no grid cost, only degradation:
```python
if solar_only_charging and power_w > 0:
    grid_cost = 0.0          # solar is free
    # degradation cost still applies (battery wear)
else:
    grid_cost = power_w * step_hours * grid_price / 1000  # normal
```

**Coordinator builds `solar_surplus_forecast_w`:**
```python
# In OptimizationCoordinator._build_optimizer_input():
if solar_only_charging:
    solar_surplus_forecast_w = [
        max(0.0, pv_forecast_w[t] - consumption_forecast_w[t])
        * solar_certainty_factor(cloud_cover[t] / 100.0)
        for t in range(num_steps)
    ]
```

The `pv_forecast` and `cloud_cover` arrays are already computed by
`ForecastCoordinator` and `WeatherDataCoordinator` respectively.

---

### Step 3 — Real-time clipping (`zero_grid_controller.py`)

Add solar-only enforcement in the dispatch path. This is the hard constraint
that prevents grid charging even if the schedule (due to forecast error) would
have requested it.

```python
# In ZeroGridController.calculate_power():
if mode == ControlMode.SOLAR_SCHEDULE:
    scheduled_w = schedule[current_step]

    if scheduled_w > 0:
        # Charging: clip to real-time solar surplus
        grid_power_w = hass.states.get(grid_sensor).state  # + = import
        pv_power_w = hass.states.get(pv_sensor).state
        house_load_w = pv_power_w - grid_power_w  # approximate
        solar_surplus_w = max(0.0, pv_power_w - house_load_w)

        # Alternative if a net-metering sensor is available:
        # solar_surplus_w = max(0.0, -grid_power_w)  # export = surplus

        actual_charge_w = min(scheduled_w, solar_surplus_w)
        send_to_inverter(actual_charge_w)

    else:
        # Discharging: follow schedule unchanged
        send_to_inverter(scheduled_w)
```

**Opportunistic extra charging (optional):**
If actual surplus exceeds the scheduled charge (more sun than forecast), the
controller can charge up to the battery's max rate rather than exporting the
excess. This can be a user toggle (`CONF_SOLAR_OPPORTUNISTIC_CHARGE`).

---

### Step 4 — New control mode (`const.py`, `select.py`, `coordinator.py`)

Add `SOLAR_SCHEDULE = "solar_schedule"` to `ControlMode` enum.

Config/translation strings needed:
- Mode name: `"Solar schedule"` / `"Zonneschema"`
- Description: charges only from solar surplus, discharges price-optimised

No new user-configurable parameters required — certainty is computed
automatically from weather data.

---

## Interaction with Rolling Re-optimization

The 15-minute `OptimizationCoordinator` cycle already provides MPC-style
adaptation. When actual solar is less than forecast:

1. Battery charges less than planned → actual SoC < planned SoC
2. Next coordinator run starts from **measured SoC** (not planned)
3. DP re-optimises discharge schedule based on available stored energy
4. Shadow price (`λ = -dV[0]/dSoC`) reflects the updated marginal value

This means forecast errors are automatically corrected within 15 minutes,
without any explicit error-correction logic needed.

---

## Files to Change

| File | Change |
|---|---|
| `forecast_models.py` | Add `solar_certainty_factor()` |
| `optimizer.py` | Add `solar_only_charging` flag, per-timestep charge limit, zero grid cost for solar charging |
| `coordinator.py` | Compute `solar_surplus_forecast_w` with certainty adjustment; pass to optimizer |
| `zero_grid_controller.py` | Add `SOLAR_SCHEDULE` dispatch path with real-time clipping |
| `const.py` | Add `ControlMode.SOLAR_SCHEDULE` |
| `select.py` | Add mode to control mode selector entity |
| `translations/en.json` + `nl.json` | Add mode label and description |
| `strings.json` | Keep in sync with `en.json` |
| `docs/analyzer.js` | Sync DP changes (solar_only_charging flag, cost formula) |
| `simulate/simulate_diagnostics.py` | Sync DP changes |

---

## Out of Scope

- Stochastic / scenario-based DP (overkill given 15-min re-planning cycle)
- PV array orientation as input to certainty (second-order effect; already
  absorbed into `pv_forecast`)
- Separate certainty factor per PV array (surplus is aggregate anyway)

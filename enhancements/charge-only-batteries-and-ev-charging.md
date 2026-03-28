# Enhancement: Charge-Only Batteries and EV Deadline Charging

## Overview

Add support for batteries that can only charge (not discharge), with optional
deadline-constrained charging — most notably for EV integration where a target
SoC must be reached by a specific departure time.

## Motivation

Current architecture assumes all batteries can both charge and discharge for
price arbitrage. Real-world use cases that don't fit this model:

- **EVs**: must reach a target SoC by departure, cannot export to grid (no V2G)
- **Thermal storage** (hot water boiler, heat-pump buffer): one-way, charged by
  electricity, "discharged" as future consumption offset
- **Regulatory/contractual restrictions**: some grid contracts or subsidy schemes
  prohibit battery feed-in
- **Controlled EV charging**: optimize *when* to charge cheaply, but never
  discharge

---

## Part 1: Charge-Only Battery Support

### What changes

**`battery_model.py` — `BatteryConfig`:**

Add `charge_only: bool = False` and `min_charge_power_kw: float = 0.0`.
When `charge_only` is `True`:
- `max_discharge_power_kw` is forced to `0.0`
- Terminal condition in the DP uses average consumption price instead of
  feed-in price (stored energy offsets future consumption, not grid export)
- UI hides discharge-related settings

`min_charge_power_kw` applies to any battery type (standard, charge-only, EV).
It represents a hardware minimum: the device must charge at this power or not
at all (e.g. EV charger minimum 6 A ≈ 1.38 kW single-phase, boiler element
minimum 2 kW). See Part 1b for the algorithmic consequences.

**`optimizer.py` — terminal condition:**

```python
# Current (for discharge-capable batteries):
terminal_price = min(feed_in_forecast[-1], avg_tail_feed_in)

# Charge-only batteries: value stored energy at avg consumption price,
# since it will offset future grid purchases, not be sold back.
if battery_config.charge_only:
    lookback = min(6, len(price_forecast))
    terminal_price = sum(price_forecast[-lookback:]) / lookback
```

**`zero_grid_controller.py`:**

Never issue a discharge command to a charge-only battery, even when the
controller is trying to balance grid export.

**`battery_model.py` — `aggregate_battery_configs`:**

`sum(max_discharge_power_kw)` correctly yields `0` for any charge-only battery.
`min_charge_power_kw` is not meaningful after aggregation (the aggregate acts as
one virtual battery); keep the field as `0.0` on the combined config and let
each per-battery controller enforce its own minimum.

**Coordinator:**

Filter disconnected batteries before aggregation (see Part 3).

---

## Part 1b: Minimum Charge Power

### The discontinuous action space problem

`min_charge_power_kw > 0` makes the charge action space **discontinuous**:

```
valid charge actions = {0} ∪ [min_charge_power_kw, max_charge_power_kw]
```

The range `(0, min_charge_power_kw)` is physically forbidden — the hardware
either charges at the minimum or is switched off. This is different from the
existing SoC-dependent derating, which only reduces the upper limit.

### `optimizer.py` — action generation

```python
# Current: evenly spaced from 0 to max
charge_actions = [float(i * power_step_w) for i in range(charge_steps, -1, -1)]

# With minimum charge power: skip the forbidden range
min_charge_w = battery_config.min_charge_power_kw * 1000
charge_actions = [0.0] + [
    float(i * power_step_w)
    for i in range(charge_steps, 0, -1)
    if i * power_step_w >= min_charge_w
]
```

The `0.0` action (idle) remains always available — the battery can always
choose not to charge.

### Interaction with the deadline constraint

The time-varying SoC floor (Part 2) must also account for the minimum charge
power. Use `min_charge_power_kw` (not `max_charge_power_kw`) when computing
how much energy *must* flow to meet the deadline:

```python
# Minimum energy available per step when we must charge (worst case: full steps
# at min power), used to enforce the floor only when min power suffices.
# The floor calculation uses max power for the optimistic reachability check.
```

If the minimum charge power is so high that it would overshoot the target SoC
(e.g. boiler nearly full, min power = 2 kW, only 15 min left), the optimizer
must idle even if it "wants" to charge a little. This is handled naturally by
the action filter above — fractional charge below `min_charge_power_kw` is
simply not in the action set.

### `zero_grid_controller.py`

When issuing a charge command, the computed setpoint must be snapped:

```python
if 0 < commanded_kw < battery_config.min_charge_power_kw:
    # Cannot charge below minimum — either snap up to minimum or go idle
    # depending on whether the grid can absorb the minimum power
    commanded_kw = battery_config.min_charge_power_kw  # or 0.0
```

The policy decision (snap up vs. idle) depends on context: if charging is
urgently needed (SoC below deadline floor), snap up; otherwise idle.

### `aggregate_battery_configs`

`min_charge_power_kw` is not summed — it is a per-device hardware limit and
has no meaningful aggregate. Each battery's real-time controller enforces its
own minimum independently.

### Config flow / UI

Show `min_charge_power_kw` for all battery types. Suggested defaults:

| Battery type | Suggested default |
|---|---|
| Standard home battery | 0 kW (no minimum) |
| EV (single-phase 16 A) | 1.4 kW (6 A × 230 V) |
| EV (three-phase) | 4.1 kW (6 A × 3 × 230 V) |
| Boiler / thermal | 1–2 kW (element rating) |

---

## Part 2: EV Deadline-Constrained Charging

### New dataclass: `ChargingConstraint`

```python
@dataclass
class ChargingConstraint:
    target_soc_kwh: float   # e.g. 0.80 × capacity_kwh
    deadline: datetime      # e.g. tomorrow 07:30
    priority: str = "hard"  # "hard" = INF cost; "soft" = large penalty
```

This is intentionally separate from `BatteryConfig`. `BatteryConfig` describes
battery physics; `ChargingConstraint` describes the user's intent for a
session.

### Algorithm change: time-varying SoC floor

Before the backward-induction pass, pre-compute the minimum SoC required at
each time step to still be able to reach `target_soc_kwh` by `t_deadline_idx`:

```python
# For t in [0, t_deadline_idx):
#   min_reachable_soc[t] = target_soc_wh
#                        - max_charge_w × sqrt_rte × Σ step_h[t..t_deadline]
min_reachable_soc = [0.0] * n_steps
if constraint and t_deadline_idx is not None:
    remaining = 0.0
    for t in range(t_deadline_idx - 1, -1, -1):
        remaining += step_durations_hours[t]
        floor = constraint.target_soc_kwh * 1000 - (
            battery_config.max_charge_power_kw * 1000
            * sqrt_rte * remaining
        )
        min_reachable_soc[t] = max(min_soc_wh, floor)
```

In the inner DP loop, mark states below the floor as infeasible:

```python
for s_idx, soc_wh in enumerate(soc_states):
    if t < t_deadline_idx and soc_wh < min_reachable_soc[t]:
        # Cannot reach target from here — skip (leave V[t][s] = INF)
        continue
    ...
```

After `t_deadline_idx` the constraint is inactive and normal cost minimisation
resumes.

### Unreachable target detection

Before the backward pass, check feasibility:

```python
available_charge_kwh = (
    battery_config.max_charge_power_kw
    * sqrt_rte
    * sum(step_durations_hours[:t_deadline_idx])
)
deficit_kwh = constraint.target_soc_kwh - current_soc_kwh
target_reachable = available_charge_kwh >= deficit_kwh
```

If `target_reachable` is `False`:
- **Hard priority**: charge as fast as possible (all floors set to `INF` for
  unreachable states), expose a warning in `OptimizationResult`
- **Soft priority**: drop to a large penalty; optimizer fills as much as
  economically rational

Expose result via new `OptimizationResult` fields:
- `ev_target_reachable: bool`
- `ev_projected_soc_at_deadline_kwh: float`

### `optimize_battery_schedule` signature

```python
def optimize_battery_schedule(
    battery_config: BatteryConfig,
    current_soc_kwh: float,
    ...,
    charging_constraint: ChargingConstraint | None = None,
) -> OptimizationResult:
```

The coordinator converts `deadline: datetime` to `t_deadline_idx: int` using
the step boundary timestamps before calling the optimizer.

---

## Part 3: EV Connection State

An EV that is not plugged in must be completely absent from the optimizer —
its capacity, power limits, and constraints must not be included.

**`BatteryConfig`:**

```python
connected_sensor_entity_id: str | None = None
```

**Coordinator:**

Before building the aggregate config, filter out batteries whose connection
sensor reads `False`:

```python
active_configs = [
    cfg for cfg in battery_configs
    if not cfg.connected_sensor_entity_id
    or hass.states.get(cfg.connected_sensor_entity_id).state == STATE_ON
]
aggregate = aggregate_battery_configs(active_configs)
```

---

## Part 4: User Configuration

### Persistent defaults (config flow, battery subentry)

| Setting | Description |
|---|---|
| Battery type | `standard` / `charge_only` / `ev` |
| Default target SoC % | e.g. 80% |
| Default departure time | e.g. 07:30 |
| Departure days | Weekdays / Every day / Custom |
| Connection sensor | `binary_sensor.ev_charger_connected` (optional) |
| Deadline priority | Hard / Soft |

When `battery_type = ev`, discharge-related settings (max discharge power,
low-SoC derating) are hidden in the UI.

### Dynamic session overrides (HA entities, read by coordinator)

| Entity | Purpose |
|---|---|
| `number.ev_target_soc_percent` | Override target SoC for this session |
| `input_datetime.ev_departure` | Override departure time for tonight/tomorrow |

Selection logic: if the override entity exists and was set within the current
session (since last disconnect), use it; otherwise fall back to the persistent
default.

### New diagnostic sensors

| Sensor | Value |
|---|---|
| `binary_sensor.ev_target_reachable` | Whether target SoC can still be met |
| `sensor.ev_projected_soc_at_departure` | Expected SoC at departure time |

### Calendar integration (future, phase 2)

Read departure time from an HA calendar entity. Requires a dedicated
"departures" calendar — generic calendar parsing is too ambiguous.

---

## Part 5: Multiple EVs

Each EV is a separate battery subentry with its own `ChargingConstraint`.
`aggregate_battery_configs` sums capacities and power limits as usual.
The coordinator passes per-battery constraints individually to the optimizer
before aggregation, or runs a separate per-battery optimization pass for
scheduling purposes while using the aggregate for real-time dispatch.

---

## Files to Change

| File | Change |
|---|---|
| `battery_model.py` | `charge_only`, `min_charge_power_kw`, `connected_sensor_entity_id` on `BatteryConfig`; new `ChargingConstraint` dataclass |
| `optimizer.py` | Discontinuous charge action space for `min_charge_power_kw`; `charging_constraint` parameter; time-varying SoC floor; charge-only terminal condition; `ev_target_reachable` in result |
| `coordinator_optimization.py` | Build `ChargingConstraint` from entities + defaults; filter disconnected batteries; pass constraint to optimizer |
| `zero_grid_controller.py` | Block discharge for `charge_only` batteries; snap charge setpoint to `min_charge_power_kw` or idle |
| `config_flow.py` | `battery_type` selector; EV-specific fields; hide discharge fields for charge-only |
| `sensor.py` | `ev_target_reachable`, `ev_projected_soc_at_departure` sensors |
| `const.py` | New config keys for all new fields |
| `translations/en.json` + `nl.json` | New strings for all new fields and sensors |
| `strings.json` | Keep in sync with `en.json` |
| `docs/analyzer.js` | Sync: discontinuous action space, time-varying SoC floor, charge-only terminal condition |
| `simulate/simulate_diagnostics.py` | Sync: same algorithm changes |
| `ALGORITHM.md` | Document deadline-constraint mechanism and charge-only terminal condition |

---

## Open Questions

1. **Thermal storage**: modelling "discharge as consumption offset" requires a
   fundamentally different cost function (no grid exchange on discharge side).
   Deferred to a separate enhancement.

2. **Soft deadline with partial charging**: if target is unreachable, should
   the optimizer aim for maximum reachable SoC, or just minimize cost and
   accept a lower SoC? Configurable via `priority`.

3. **Post-deadline behaviour when still connected**: resume normal charge-only
   optimisation (cheap hour top-up) or hold at target SoC? Probably
   configurable, defaulting to resume.

4. **V2G (Vehicle-to-Grid)**: treat as a standard bidirectional battery with
   a deadline constraint and `charge_only = False`. No special handling needed
   beyond the constraint mechanism above.

5. **Minimum charge power: snap up or idle?** When real-time power falls below
   `min_charge_power_kw`, the controller must choose: snap up to the minimum
   (risk importing slightly more than optimal) or go idle (risk missing the
   deadline floor). The right policy depends on deadline urgency and grid state.
   A simple heuristic: snap up if remaining time to deadline puts SoC below
   the reachability floor, otherwise idle.

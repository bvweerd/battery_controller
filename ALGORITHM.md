# Algorithm: How Battery Controller Optimizes Your Schedule

This document explains step by step how the dynamic programming (DP) engine in Battery Controller calculates the optimal charge/discharge schedule for your battery.

---

## Table of Contents

1. [Problem Statement](#1-problem-statement)
2. [Inputs](#2-inputs)
3. [State Space Discretization](#3-state-space-discretization)
4. [Cost Function (Step Cost)](#4-cost-function-step-cost)
5. [Terminal Condition](#5-terminal-condition)
6. [Backward Pass (Backward Induction)](#6-backward-pass-backward-induction)
7. [Forward Pass (Schedule Extraction)](#7-forward-pass-schedule-extraction)
8. [Post-Processing Filters](#8-post-processing-filters)
9. [Shadow Price Calculation](#9-shadow-price-calculation)
10. [Rolling-Horizon Execution](#10-rolling-horizon-execution)
11. [Numerical Considerations](#11-numerical-considerations)

---

## 1. Problem Statement

The optimizer answers this question for every 15-minute run:

> **Given the current battery state of charge (SoC) and forecasts of prices, PV production, and household consumption for the next N hours, what charge/discharge power should the battery apply at each time step to minimize total electricity cost?**

"Total cost" includes:
- Grid import costs (buying electricity)
- Grid export revenue (selling electricity, negative cost)
- Battery degradation cost (wear per full charge+discharge cycle)

The optimizer must respect physical constraints: SoC must stay within `[min_soc, max_soc]`, and power must stay within `[-max_discharge, max_charge]`.

---

## 2. Inputs

| Input | Unit | Description |
|-------|------|-------------|
| `current_soc_kwh` | kWh | Current battery state of charge |
| `price_forecast[t]` | EUR/kWh | Grid buy price for each time step |
| `feed_in_forecast[t]` | EUR/kWh | Grid sell (export) price for each time step |
| `pv_forecast[t]` | kW | AC-side PV production for each time step |
| `pv_dc_forecast[t]` | kW | DC-coupled PV production for each step (optional) |
| `consumption_forecast[t]` | kW | Expected household consumption for each step |
| `step_durations_hours[t]` | h | Duration of each time step (typically 0.25 h = 15 min) |
| `degradation_cost_per_cycle` | EUR/cycle | Battery wear cost per full charge+discharge cycle (converted to EUR/kWh by coordinator: `÷ usable_kwh`) |
| `min_price_spread` | EUR/kWh | Minimum buy/sell spread to trigger arbitrage |
| `terminal_shadow_price` | EUR/kWh | Marginal value of stored energy from previous run (optional) |

**Battery configuration:**

| Parameter | Description |
|-----------|-------------|
| `capacity_kwh` | Total battery capacity |
| `min_soc_kwh / max_soc_kwh` | Operating SoC limits |
| `max_charge_power_kw / max_discharge_power_kw` | Power limits |
| `round_trip_efficiency` (RTE) | End-to-end efficiency (e.g. 0.90 = 90%) |
| `pv_dc_coupled` | Whether DC-coupled PV is present |
| `pv_dc_efficiency` | DC MPPT + DC-DC conversion efficiency (~0.97) |
| `max_grid_power_kw` | Grid connection cap (0 = unlimited) |

---

## 3. State Space Discretization

The DP operates on a discrete grid of SoC states and power actions. Continuous SoC and power are approximated by finite grids to make the problem tractable.

### 3.1 SoC Grid

The SoC range `[min_soc_kwh, max_soc_kwh]` is divided into evenly spaced states:

```
soc_resolution_wh = max(SOC_RESOLUTION_WH, POWER_STEP_W × min_step_hours × sqrt(RTE))
n_soc_states = round((max_soc_wh - min_soc_wh) / soc_resolution_wh) + 1
soc_states[i] = min_soc_wh + i × soc_resolution_wh
```

- **`SOC_RESOLUTION_WH`** is the minimum resolution (default: 100 Wh).
- The resolution is also bounded from below by the energy moved by one power step in the shortest time interval, scaled by `sqrt(RTE)`. This ensures that every discretized action changes the SoC by at least one state.
- SoC boundaries are rounded to the nearest Wh to prevent floating-point comparison errors (e.g. `212.0 < 212.00000000000003`).

### 3.2 Power Action Grid

Actions are discretized in steps of `power_step_w`:

```
power_step_w = max(POWER_STEP_W, soc_resolution_wh / full_step_hours)
charge_actions   = [max_charge_w, ..., 2×step, step, 0]   (highest-first)
discharge_actions = [-step, -2×step, ..., -max_discharge_w] (lowest-first)
actions = discharge_actions + charge_actions
```

- `full_step_hours` is the duration of a regular (non-partial) time step. A partial first step (e.g. 3 minutes remaining before the next price boundary) must not shrink `power_step_w` for all subsequent full steps.
- Charge actions are listed highest-first so that the DP's "first equal wins" tie-breaking naturally produces **front-loaded charging** (maximum power immediately rather than a ramp-up).

---

## 4. Cost Function (Step Cost)

`calculate_step_cost(t, s, a)` computes the cost of applying action `a` (W) at time step `t` with SoC `s` (Wh). All values in this section refer to a single time step of duration `dt` hours.

### 4.1 Efficiency Model

Round-trip efficiency is split symmetrically between charge and discharge:

```
charge_efficiency    = sqrt(RTE)
discharge_efficiency = sqrt(RTE)
```

So charge × discharge = RTE end-to-end. For RTE = 0.90: each direction is ~0.9487.

The efficiency is treated as constant across all power levels and SoC values. C-rate-dependent losses (I²R) and SoC-dependent losses are deliberately omitted: literature confirms both effects are negligible for LFP within the 10–90% SoC operating window and at the C-rates typical for home storage (≤ 0.5C). The user-configured RTE already accounts for real-world losses at typical operating conditions.

DC-coupled PV uses its own path efficiency (`pv_dc_efficiency` ≈ 0.97, MPPT only, no AC conversion).

### 4.2 Charging (`action_w > 0`)

When charging at power `P` (W):

1. **Use DC PV first** (free energy, higher efficiency):
   ```
   dc_charge_w = min(P, pv_dc_production_w × dc_eff)
   ac_charge_w = P - dc_charge_w
   ```

2. **AC grid draw** for the remainder:
   ```
   grid_to_battery_w = ac_charge_w / charge_eff
   ```

3. **Remaining DC PV** not absorbed by battery flows to AC through the inverter at 96% efficiency.

4. **Throughput**: `P × dt / 1000` kWh (for degradation).

### 4.3 Discharging (`action_w < 0`)

When discharging at setpoint `P` (W, negative):

```
usable_power_w   = |P| × discharge_eff    (power delivered to home AC side)
grid_to_battery_w = -usable_power_w       (negative = flows away from grid)
throughput_kwh    = |P| × dt / 1000
```

All DC PV excess flows to AC while discharging.

### 4.4 Idle (`action_w == 0`)

No explicit grid power, but DC-coupled PV still passively charges the battery up to available headroom:

```
headroom_wh      = max_soc_wh - soc_wh
passive_charge_wh = min(pv_dc_production_w × dc_eff × dt, headroom_wh)
throughput_kwh    = passive_charge_wh / 1000
```

### 4.5 Net Grid Exchange and Final Cost

```
total_ac_pv_w = pv_production_w + dc_pv_excess_w × DC_TO_AC_INVERTER_EFF
net_grid_w    = consumption_w - total_ac_pv_w + grid_to_battery_w
```

If `max_grid_power_kw > 0`, `net_grid_w` is clamped to `[-cap, +cap]`.

```
energy_kwh = |net_grid_w| × dt / 1000

grid_cost = energy_kwh × grid_price      (if net_grid_w > 0: buying)
          = -energy_kwh × feed_in_price  (if net_grid_w < 0: selling)

degradation_cost_per_kwh = degradation_cost_per_cycle / usable_kwh  (conversion in coordinator)
degradation_cost = throughput_kwh × degradation_cost_per_kwh

step_cost = grid_cost + degradation_cost
```

---

## 5. Terminal Condition

The DP looks `N` steps into the future. At time `T` (end of horizon), the battery still holds energy. Without a terminal value, the DP would irrationally discharge the battery just before the horizon ends (energy "disappears" after step N).

The terminal value of being in SoC state `s` is:

```
V[T][s] = -(stored_kwh × terminal_price)
```

where `stored_kwh = (soc_wh - min_soc_wh) / 1000`.

The negative sign is because `V` represents cost — more stored energy means lower cost (more future value).

**Choosing `terminal_price`:**

1. **Preferred**: `terminal_shadow_price` from the previous optimizer run. This is the marginal value of stored energy derived from the full price structure (see [Section 9](#9-shadow-price-calculation)). It is more stable than a single end-of-horizon price.
2. **Fallback**: `min(feed_in_forecast[-1], average of last 6 feed-in prices)` — blended tail to dampen transient price spikes at the forecast boundary.

---

## 6. Backward Pass (Backward Induction)

This is the core of the algorithm. It computes the **value function** `V[t][s]`: the minimum achievable total cost from time `t` to the end of the horizon, starting in SoC state `s`.

The Bellman equation is:

```
V[t][s] = min over all actions a of:
               step_cost(t, s, a) + V[t+1][s']
```

where `s'` is the SoC state after applying action `a`:

- **Charging**: `s' = s + a × dt × sqrt(RTE)` (energy stored in battery)
- **Discharging**: `s' = s - |a| × dt / sqrt(RTE)` (energy drawn from battery, with losses)
- **Idle**: `s' = s` (or passive DC PV charging if DC-coupled)

The backward pass runs from `t = N-1` down to `t = 0`:

```python
for t in range(N-1, -1, -1):
    for s in all_soc_states:
        best_cost = infinity
        best_action = 0

        for a in all_actions:
            s' = transition(s, a, t)
            if s' < min_soc or s' > max_soc:
                continue   # constraint violation
            if a != 0 and nearest_soc_idx(s') == nearest_soc_idx(s):
                continue   # sub-resolution action — skip to avoid ghost "free" moves

            cost = step_cost(t, s, a) + V[t+1][nearest_soc_idx(s')]
            if cost < best_cost:
                best_cost = cost
                best_action = a

        V[t][s] = best_cost
        policy[t][s] = best_action
```

**Sub-resolution skip rule**: If a non-zero action does not move the SoC to a different discrete state, the DP sees it as "free" (no change in `V[t+1]`) but the step cost still includes RTE losses. Allowing such actions causes spurious micro-charging/discharging at zero apparent benefit. These actions are skipped. Idle (`a = 0`) is exempt because passive DC PV may still move the SoC bin.

After the backward pass, `V[0][s]` gives the optimal total cost from now to the horizon for each starting SoC, and `policy[t][s]` gives the optimal action.

---

## 7. Forward Pass (Schedule Extraction)

Starting from the current SoC (`current_soc_wh` → nearest discrete state), the forward pass traces the optimal trajectory:

```python
current_soc = nearest_soc_state(current_soc_kwh)

for t in range(N):
    s_idx = nearest_soc_idx(current_soc)
    a = policy[t][s_idx]
    record a as power_schedule_kw[t]

    if a > 0:   # charging
        current_soc += a × dt × sqrt(RTE)
        mode = "charging"
    elif a < 0: # discharging
        current_soc -= |a| × dt / sqrt(RTE)
        mode = "discharging"
    else:       # idle (passive DC PV if applicable)
        current_soc += passive_dc_pv_charge
        mode = "idle"

    soc_schedule_kwh[t+1] = current_soc / 1000
```

This gives:
- `power_schedule_kw[t]` — battery power at each step (positive = charge)
- `mode_schedule[t]` — `"charging"`, `"discharging"`, or `"idle"`
- `soc_schedule_kwh[t]` — expected SoC at the start of each step

---

## 8. Post-Processing Filters

After the forward pass, two filters clean up the schedule.

### 8.1 Oscillation Filter

The DP sometimes schedules rapid charge↔discharge switches that are technically cost-optimal within the discrete state space but produce excessive battery cycling with little financial benefit.

**Minimum profitable spread**: For arbitrage to be worthwhile, the discharge price must exceed the charge price by at least:

```
min_arbitrage_spread = (2 × degradation_cost_per_kwh + min_price_spread) / sqrt(RTE)
```

This accounts for RTE losses in both directions and the user-configured minimum spread.

**Algorithm**: Iterative lookahead scan (repeated until no more changes):

```
for each step i:
    if mode[i] == "charging":
        search steps i+1 to i+window for the next "discharging" step j
        effective_spread = discharge_price[j] - charge_cost[i] / RTE
        if effective_spread < min_arbitrage_spread:
            suppress step i (set to idle)
            mark changed = True

    if mode[i] == "discharging":
        search steps i+1 to i+window for the next "charging" step j
        effective_spread = discharge_price[i] - charge_cost[j] / RTE
        if effective_spread < min_arbitrage_spread:
            suppress step i (set to idle)
            mark changed = True
```

When there is a **PV surplus** at a charging step (PV production > consumption), the charge cost is the feed-in opportunity cost (what could have been earned by exporting that PV) rather than the grid buy price.

The window size is `max(2 h, battery_capacity / max_discharge_power)` — larger batteries need a wider window because a full charge/discharge cycle takes longer.

After suppression, the SoC schedule is recalculated to remain consistent.

### 8.2 Micro-Cycle Filter

Very short charge or discharge segments (e.g. a single 15-minute slot) move so little energy that degradation cost per kWh becomes disproportionately high. Any contiguous block of charging or discharging that moves less than `MIN_CYCLE_KWH` (default: 0.2 kWh) is replaced with idle.

---

## 9. Shadow Price Calculation

The **shadow price** λ is the marginal value of one additional kWh stored in the battery right now. It is derived numerically from the value function using a central difference:

```
λ = -dV[0]/dSoC = (V[0][s-1] - V[0][s+1]) / (2 × ΔSoC_kWh)
```

- `V` is cost (lower is better), so adding energy reduces cost → the gradient is negative → λ is positive.
- For boundary SoC states, a one-sided difference is used.

**Interpretation**:
- If the current grid buy price is less than `λ / sqrt(RTE)`, it is profitable to charge — the stored energy is worth more than its purchase cost.
- If the current feed-in price is greater than `λ × sqrt(RTE)`, it is profitable to discharge/export.

**Use as terminal condition**: λ is passed to the next optimizer run as `terminal_shadow_price`, replacing the end-of-horizon feed-in price in the terminal condition. This makes consecutive 15-minute runs consistent with each other (rolling-horizon stability).

---

## 10. Rolling-Horizon Execution

The optimizer runs every 15 minutes. Each run:

1. Reads the current SoC and latest forecasts.
2. Calls `optimize_battery_schedule()` with the previous run's shadow price as `terminal_shadow_price`.
3. Executes **only the first step** of the resulting schedule (`optimal_power_kw`).
4. Saves the new shadow price for the next run.

This is called a **rolling horizon** or **receding horizon** approach. Re-optimizing every 15 minutes allows the schedule to adapt as prices are updated, PV production deviates from forecast, or household consumption changes.

The shadow price acts as a **bridge between runs**: it encodes how much future value will be lost or gained by entering the next horizon with more or less stored energy. Without it, each run is blind to what happens after the forecast ends, potentially draining the battery just before prices spike.

### Convergence

On the first run there is no prior shadow price, so the terminal condition falls back to the tail average of the feed-in forecast. As runs accumulate over days and weeks:
- The shadow price converges to a value that reflects typical overnight vs. daytime price patterns and seasonal PV output.
- The historical price model (which provides forecasts before day-ahead prices are published) also improves with more recorder data.

During the convergence period (first 2–4 weeks), the schedule may change more noticeably between runs and may appear more aggressive than the long-run optimum.

---

## 11. Numerical Considerations

Several implementation details prevent subtle correctness bugs:

| Issue | Fix |
|-------|-----|
| Floating-point SoC boundary | `min_soc_wh = round(capacity × min_pct / 100 × 1000)` — avoids `212.0 < 212.00000000000003` |
| Sub-resolution actions | Skip any non-idle action that doesn't cross a SoC state boundary |
| Partial first step | Use `step_durations_hours[1]` (not `min(step_durations_hours)`) to compute `power_step_w` |
| Horizon-end discharge | Terminal condition `V[T][s] = -stored_kwh × terminal_price` prevents horizon-end drain |
| Feed-in price `None` | Coordinator always falls back to `CONF_FIXED_FEED_IN_PRICE`; returning `None` would cause the DP to use the grid buy price, making PV arbitrage always unprofitable |
| RTE symmetry | `charge_eff = discharge_eff = sqrt(RTE)` ensures `charge_eff × discharge_eff = RTE` exactly |

---

## Summary

```
Inputs: SoC, price/PV/consumption forecasts, battery config
         │
         ▼
[3] Discretize SoC (grid of states) and power (grid of actions)
         │
         ▼
[5] Set terminal condition V[T][s] = -(stored_kwh × terminal_price)
         │
         ▼
[6] BACKWARD PASS (t = N-1 → 0)
    For each (t, s): V[t][s] = min_a { step_cost(t, s, a) + V[t+1][s'] }
    Store optimal action in policy[t][s]
         │
         ▼
[7] FORWARD PASS
    Trace policy from current SoC → power/mode/SoC schedule
         │
         ▼
[8] Post-processing
    Oscillation filter: remove unprofitable charge↔discharge switches
    Micro-cycle filter: suppress tiny charge/discharge blocks
         │
         ▼
[9] Compute shadow price λ from V[0] gradient
         │
         ▼
Output: power_schedule_kw, mode_schedule, soc_schedule_kwh,
        shadow_price_eur_kwh, savings estimate
         │
         ▼
[10] Execute step 0, save λ, wait 15 min, repeat
```

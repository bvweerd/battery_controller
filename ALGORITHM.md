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
12. [Multi-Battery Dispatch](#12-multi-battery-dispatch)

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
| `degradation_cost_per_cycle` | EUR/cycle | Battery wear cost per full charge+discharge cycle (converted to EUR/kWh by coordinator: `÷ (2 × usable_kwh)` — one cycle is charge + discharge throughput) |
| `min_price_spread` | EUR/kWh | Minimum buy/sell spread to trigger arbitrage |
| `terminal_shadow_price` | EUR/kWh | Marginal value of stored energy from previous run (optional) |

**Battery configuration:**

| Parameter | Description |
|-----------|-------------|
| `capacity_kwh` | Total battery capacity |
| `min_soc_kwh / max_soc_kwh` | Operating SoC limits |
| `max_charge_power_kw / max_discharge_power_kw` | Nominal power limits |
| `round_trip_efficiency` (RTE) | End-to-end efficiency (e.g. 0.90 = 90%) |
| `pv_dc_coupled` | Whether DC-coupled PV is present |
| `pv_dc_efficiency` | DC MPPT + DC-DC conversion efficiency (~0.97) |
| `max_grid_power_kw` | Grid connection cap (0 = unlimited) |
| `high_soc_charge_threshold_pct` | Above this SoC (%), charge is limited to `high_soc_max_charge_kw` |
| `high_soc_max_charge_kw` | Derated charge limit above threshold (0 = no derating) |
| `low_soc_discharge_threshold_pct` | Below this SoC (%), discharge is limited to `low_soc_max_discharge_kw` |
| `low_soc_max_discharge_kw` | Derated discharge limit below threshold (0 = no derating) |

---

## 3. State Space Discretization

The DP operates on a discrete grid of SoC states and power actions. Continuous SoC and power are approximated by finite grids to make the problem tractable.

### 3.1 SoC Grid

The SoC range `[min_soc_kwh, max_soc_kwh]` is divided into evenly spaced states:

```
soc_resolution_wh = SOC_RESOLUTION_WH
n_soc_states = round((max_soc_wh - min_soc_wh) / soc_resolution_wh) + 1
soc_states[i] = min_soc_wh + i × soc_resolution_wh
```

- **`SOC_RESOLUTION_WH`** (default: 10 Wh) is the only grid constant; the power step is derived from it.
- SoC boundaries are rounded to the nearest Wh to prevent floating-point comparison errors (e.g. `212.0 < 212.00000000000003`).

### 3.2 Power Action Grid

The power step uses `POWER_STEP_W` as a practical minimum to prevent unprofitable trickle actions at near-marginal prices. The aligned step ensures the smallest action crosses at least one SoC state:

```
aligned_step_w   = soc_resolution_wh / full_step_hours   (e.g. 10 Wh / 1 h = 10 W)
power_step_w     = max(POWER_STEP_W, aligned_step_w)      (e.g. max(100, 10) = 100 W)
charge_actions   = [max_charge_w, ..., 2×step, step, 0]   (highest-first)
discharge_actions = [-step, -2×step, ..., -max_discharge_w] (lowest-first)
actions = discharge_actions + charge_actions
```

**Boundary actions** are evaluated separately for each SoC state after the main action loop to capture the residual capacity that the 100 W grid cannot reach (up to ~115 Wh per step):

```
drain_w = (soc_wh - min_soc_wh) × sqrt_RTE / step_hours   → new_soc_idx = 0
fill_w  = (max_soc_wh - soc_wh) / (step_hours × sqrt_RTE) → new_soc_idx = n_soc_states − 1
```

`new_soc_idx` is set directly (not recomputed via the energy formula) to avoid floating-point errors at exact boundaries.

- `full_step_hours` is the duration of a regular (non-partial) time step. A partial first step (e.g. 3 minutes remaining before the next price boundary) must not shrink `power_step_w` for all subsequent full steps.
- Charge actions are listed highest-first so that the DP's "first equal wins" tie-breaking naturally produces **front-loaded charging** (maximum power immediately rather than a ramp-up).
- The global action list is built from the **nominal** (unconstrained) maxima. Per-state derating is applied in the backward pass (see Section 6).

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

degradation_cost_per_kwh = degradation_cost_per_cycle / (2 × usable_kwh)  (conversion in coordinator; a full cycle = 2 × usable_kwh throughput)
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
# Pre-compute per-state power limits (outside the t-loop for performance)
soc_max_charge_w[s]    = max_charge_at_soc(soc_states[s])
soc_max_discharge_w[s] = max_discharge_at_soc(soc_states[s])

for t in range(N-1, -1, -1):
    for s in all_soc_states:
        best_cost = infinity
        best_action = 0

        for a in all_actions:
            if a > 0 and a > soc_max_charge_w[s]:
                continue   # SoC-dependent charge derating
            if a < 0 and |a| > soc_max_discharge_w[s]:
                continue   # SoC-dependent discharge derating

            s' = transition(s, a, t)
            if s' < min_soc or s' > max_soc:
                continue   # SoC boundary violation
            if a != 0 and nearest_soc_idx(s') == nearest_soc_idx(s):
                continue   # sub-resolution action — skip to avoid ghost "free" moves

            cost = step_cost(t, s, a) + V[t+1][nearest_soc_idx(s')]
            if cost < best_cost:
                best_cost = cost
                best_action = a

        V[t][s] = best_cost
        policy[t][s] = best_action
```

**SoC-dependent derating**: Some batteries (e.g. Marstek Venus A) reduce their max charge/discharge power near SoC extremes (BMS absorption). The per-state limits `soc_max_charge_w[s]` and `soc_max_discharge_w[s]` are precomputed before the outer time loop using `BatteryConfig.max_charge_at_soc()` and `max_discharge_at_soc()`. For batteries without derating (the default), these equal the nominal maxima and the guards never fire.

**Effect on the schedule**: Because backward induction propagates future costs, the DP at step `t` already "knows" that entering a derated SoC zone at `t+1` constrains future power. This causes the optimizer to front-load discharge before the low-SoC threshold — for example, discharging at 1200 W in earlier steps rather than running out of power at 380 W near empty.

**Sub-resolution skip rule**: If a non-zero action does not move the SoC to a different discrete state, the DP sees it as "free" (no change in `V[t+1]`) but the step cost still includes RTE losses. Allowing such actions causes spurious micro-charging/discharging at zero apparent benefit. These actions are skipped. Idle (`a = 0`) is exempt because passive DC PV may still move the SoC bin.

After the backward pass, `V[0][s]` gives the optimal total cost from now to the horizon for each starting SoC, and `policy[t][s]` gives the optimal action.

---

## 7. Forward Pass (Schedule Extraction)

The forward pass re-evaluates the V-table at the actual continuous SoC instead of snapping to the nearest discrete state and following the policy table. At each step it enumerates the same action set as the backward pass (including boundary actions) and picks the minimum-cost action:

```python
current_soc = current_soc_kwh * 1000  # continuous, not snapped

for t in range(N):
    soc_idx = nearest_soc_idx(current_soc)
    best_action, best_new_soc = argmin over all actions a:
        step_cost(t, current_soc, a) + V[t+1][nearest_soc_idx(new_soc_after(a))]

    record best_action as power_schedule_kw[t]
    current_soc = best_new_soc
    soc_schedule_kwh[t+1] = current_soc / 1000
```

The sub-resolution skip still applies: non-zero actions that do not cross a SoC state boundary are skipped. Boundary actions (exact drain-to-min / fill-to-max) are also evaluated as in the backward pass.

This eliminates the SoC discretisation error that accumulates when the policy for a neighbouring discrete state differs from the optimal action at the true SoC.

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

**DC-coupled PV**: For systems with DC-coupled PV, passive charging from the DC PV array occurs even when the battery is idle. The oscillation filter accounts for this by subtracting the passive DC PV contribution from the active charge power when calculating charge cost. If all charging would come from passive DC PV anyway, the effective cost is zero and the charge step is never suppressed.

The window size is `max(2 h, battery_capacity / max_discharge_power)` — larger batteries need a wider window because a full charge/discharge cycle takes longer.

After suppression, the SoC schedule is recalculated to remain consistent.

### 8.3 PV Curtailment Override

When the **PV Curtailed** switch is enabled in the integration, the coordinator zeroes both `pv_forecast` and `pv_dc_forecast` before passing them to the optimizer. This corrects for the case where solar inverters automatically shut down at negative feed-in prices, leaving the controller with a phantom PV production in its forecast.

Effect on the optimizer:
- `net_load` is higher (consumption is no longer offset by PV) → more incentive to charge at negative prices.
- DC PV passive charging is removed from SoC transitions → the DP no longer expects "free" battery top-up from the sun.
- With negative prices the optimal action is charging from the grid; zeroing PV ensures the planned charge rate is not artificially reduced by expected solar contribution.

Effect on real-time control:
- **Zero-grid mode**: instead of reacting to the live grid sensor (which would trigger discharging to cover load), the controller follows the DP schedule. With negative prices and zeroed PV that schedule is charging, not discharging.
- **Hybrid/follow-schedule**: unchanged — the corrected forecast is already reflected in the optimizer output.

This flag is a manual override, intended to be toggled by a Home Assistant automation that watches the inverter's export-limitation status sensor.

---

### 8.2 Micro-Cycle Filter

Very short charge or discharge segments (e.g. a single 15-minute slot) move so little energy that degradation cost per kWh becomes disproportionately high. Any contiguous block of charging or discharging that moves less than `MIN_CYCLE_KWH` (default: 0.2 kWh) is replaced with idle. If no micro-cycles are found, the filter returns the schedule unchanged without rebuilding.

---

## 9. Shadow Price Calculation

The **shadow price** λ is the marginal value of one additional kWh stored in the battery right now. It is derived numerically from the value function using a central difference:

```
λ = -dV[0]/dSoC = (V[0][s-1] - V[0][s+1]) / (2 × ΔSoC_kWh)
```

- `V` is cost (lower is better), so adding energy reduces cost → the gradient is negative → λ is positive.
- For boundary SoC states, a one-sided difference is used.

**Interpretation**:
- If the current grid buy price is less than `λ × sqrt(RTE)`, it is profitable to charge — 1 kWh bought from AC stores `sqrt(RTE)` kWh worth `λ` each, so the stored energy is worth more than its purchase cost.
- If the current feed-in price is greater than `λ / sqrt(RTE)`, it is profitable to discharge/export — 1 stored kWh yields `sqrt(RTE)` kWh on the AC side, so the sale revenue exceeds the value of keeping the energy.

Charge-speed correction: when runtime calibration detects that the battery gains less SoC per time step than modelled, the optimizer reduces only the **charge-side SoC transition** for planning. The economic cost model and arbitrage thresholds continue to use the nominal `sqrt(RTE)` so a charging-speed limit is not double-counted as extra energy loss.

The shadow price is always the raw DP value — there is no separate "post-processed" shadow price. Post-processing filters affect `total_cost` and `savings` (where the difference between raw and processed values shows the impact of filtered actions), but the shadow price is a DP concept that is not modified by post-processing.

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
| Partial first step | Use `step_durations_hours[1]` (not `min(step_durations_hours)`) to compute `power_step_w`; optimizer always uses step 0 as the immediate setpoint (primary runs are at period boundaries so step 0 is full) |
| Horizon-end discharge | Terminal condition `V[T][s] = -stored_kwh × terminal_price` prevents horizon-end drain |
| Feed-in price `None` | Coordinator always falls back to `CONF_FIXED_FEED_IN_PRICE`; returning `None` would cause the DP to use the grid buy price, making PV arbitrage always unprofitable |
| RTE symmetry | `charge_eff = discharge_eff = sqrt(RTE)` ensures `charge_eff × discharge_eff = RTE` exactly |
| Derating precomputed outside `t`-loop | `soc_max_charge_w[s]` and `soc_max_discharge_w[s]` are arrays computed once from `soc_states` before the backward pass, avoiding repeated method calls in the tight inner loop |

## 12. Variable Price Intervals (15-min / 30-min / hourly)

The DP is fully **interval-agnostic**: all energy quantities are scaled by `step_durations_hours[t]`, so the same algorithm handles 15-minute, 30-minute, and hourly prices without modification.

### How the interval is detected

`helpers.py` auto-detects the price interval from the sensor's attributes:

1. **Sensors with per-entry timestamps** (`net_prices_today`, `raw_today`, etc.): the delta between the first two `start` timestamps determines the interval (15, 30, or 60 min).
2. **Sensors without timestamps** (`today`, `tomorrow`, bare float lists): defaults to 60 min.

Detection happens in `_detect_interval_from_entries()`. The result flows into:
- `extract_price_forecast_with_timestamps()` → returns `(prices, start_times, interval_minutes)`
- `compute_step_durations_hours()` → computes per-step durations aligned to price boundaries

### Scheduling

The optimizer runs are event-driven, synchronised to the price sensor:

| Trigger | When | Purpose |
|---------|------|---------|
| `_handle_price_change` (period boundary) | `start_times[0]` changes | Primary run; step 0 is always a full interval |
| Mid-period correction | `period_start + interval/2` | Corrects SoC drift from the primary run |
| DC coordinator fallback | 60-min interval | Safety net if above triggers miss |

For sensors without timestamp attributes the fallback is a >10% price-change threshold.

### Step duration alignment

The first DP step covers only the **remaining time** within the current price slot (partial interval). All subsequent steps are full intervals. This prevents energy miscalculation at price boundaries.

Because primary runs fire at period boundaries, step 0 is always (or nearly always) a full interval. The partial-step case only occurs on HA restarts mid-period or during mid-period correction runs; in both cases the DP step 0 action is used directly.

| Price interval | Steps per day | First step | Full steps | DP table size |
|---|---|---|---|---|
| 60 min | 24 | ≤ 60 min | 1 h = 1.0 h each | 24 × SoC states |
| 30 min | 48 | ≤ 30 min | 0.5 h each | 48 × SoC states |
| 15 min | 96 | ≤ 15 min | 0.25 h each | 96 × SoC states |

### PV and consumption resampling

PV production and household consumption forecasts are always computed at 60-minute resolution (the open-meteo weather API delivers hourly data). When the price interval is finer, these are resampled to match:

- `resample_forecast(pv_forecast_kw, 60, price_interval)` — weighted-average downsampling
- Each 15-min slot within an hour gets the same kW value as the parent hour

### Past-entry exclusion for timestamp-bearing sensors

For sensors like Nordpool `raw_today` (96 entries per day with explicit `start` timestamps), past entries are skipped by comparing each entry's `start + interval` against `now`. This is identical to the `net_prices_today` handling and correctly excludes elapsed 15-min slots regardless of the current minute within an hour.

---

## 13. Baseline Cost and Savings

The **baseline cost** represents the electricity cost without any battery. In this scenario:
- All consumption is met directly from the grid
- All AC PV surplus is exported at the feed-in price
- DC-coupled PV panels still produce, but without a battery to absorb the DC output, all DC PV goes through the inverter to AC (at `DC_TO_AC_INVERTER_EFFICIENCY`)

**Raw vs processed costs**: The optimizer reports both raw (DP-derived) and post-processed costs:
- `raw_total_cost`: V[0][current_soc_idx] from the DP value function
- `total_cost`: Recalculated from the filtered schedule via `_calculate_schedule_total_cost`
- The difference shows the impact of post-processing filters (typically < 0.01 EUR when no actions are filtered, due to floating-point differences in SoC reconstruction)
- `savings = baseline_cost - initial_terminal_value - total_cost`

---

## 14. Summary

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

---

## 12. Multi-Battery Dispatch

The DP optimizer treats all configured batteries as a single aggregated virtual battery (`aggregate_battery_configs`). After the optimizer produces a combined setpoint, `_split_setpoint` distributes it across the individual inverters.

### SoC-gap triggered concentration

The dispatch strategy is determined by the **relative-SoC gap** between batteries:

```
rel_soc_i = (soc_i - min_soc_i) / (max_soc_i - min_soc_i)   ∈ [0, 1]
gap = max(rel_soc) − min(rel_soc)
```

| Gap | Strategy |
|-----|----------|
| `gap < 0.10` | **Concentrate** on one battery |
| `gap ≥ 0.10` | **Proportional split** to rebalance |

**Why concentration at low gap:** splitting a small setpoint (e.g. 100 W) across two inverters results in each receiving ~50 W. Most home battery inverters are inefficient at very low power (high fixed conversion losses relative to throughput). Concentrating on one inverter avoids this and provides more stable real-time tracking in zero-grid mode.

**Why proportional split at high gap:** when SoC has diverged, one battery is being disproportionately cycled. Proportional splitting (weighted by available headroom/energy) rebalances the SoC levels over time.

### Battery selection for concentration

When concentrating, which battery is chosen depends on the control mode:

| Mode | Selection criterion |
|------|---------------------|
| `zero_grid` / `hybrid` | Closest to 50% rel\_soc — can handle charge and discharge direction changes longest without hitting a SoC limit |
| Scheduled charge | Lowest rel\_soc (most headroom) |
| Scheduled discharge | Highest rel\_soc (most energy available) |

**Hysteresis:** the active battery only changes when the best candidate's score exceeds the current battery's score by more than `_SOC_HYSTERESIS = 0.05`. This creates three zones:

```
gap < 0.05   → concentrate, no switch (hysteresis holds)
0.05 ≤ gap < 0.10 → concentrate, switch to better battery
gap ≥ 0.10   → proportional split, reset active battery selection
```

### Power-overflow redistribution

Within the proportional split path, batteries that reach their `max_charge_power_kw` or `max_discharge_power_kw` limit have the overflow redistributed iteratively to the remaining batteries. This ensures the combined setpoint is fully absorbed even when one inverter is at its power limit.

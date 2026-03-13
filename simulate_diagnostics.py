#!/usr/bin/env python3
"""
Battery schedule simulator: loads diagnostics.json and re-runs the optimizer,
then explains each scheduling decision step by step.

Usage:
    python simulate_diagnostics.py [diagnostics.json]

Helps diagnose why the optimizer made certain charge/discharge decisions.
"""

import json
import math
import sys
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Inline battery model (to avoid HA import dependencies)
# ---------------------------------------------------------------------------


@dataclass
class BatteryConfig:
    capacity_kwh: float
    usable_capacity_kwh: float
    max_charge_power_kw: float
    max_discharge_power_kw: float
    round_trip_efficiency: float
    charge_efficiency: float
    discharge_efficiency: float
    min_soc_percent: float
    max_soc_percent: float
    min_soc_kwh: float
    max_soc_kwh: float
    pv_dc_coupled: bool
    pv_dc_peak_power_kwp: float
    pv_dc_efficiency: float
    max_grid_power_kw: float = 0.0


# ---------------------------------------------------------------------------
# Load diagnostics
# ---------------------------------------------------------------------------


def load_diagnostics(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def extract_inputs(diag: dict) -> tuple:
    """Extract all optimizer inputs from diagnostics.json."""
    bc = diag["data"]["battery_config"]
    battery = BatteryConfig(
        capacity_kwh=bc["capacity_kwh"],
        usable_capacity_kwh=bc["usable_capacity_kwh"],
        max_charge_power_kw=bc["max_charge_power_kw"],
        max_discharge_power_kw=bc["max_discharge_power_kw"],
        round_trip_efficiency=bc["round_trip_efficiency"],
        charge_efficiency=bc["charge_efficiency"],
        discharge_efficiency=bc["discharge_efficiency"],
        min_soc_percent=bc["min_soc_percent"],
        max_soc_percent=bc["max_soc_percent"],
        min_soc_kwh=bc["min_soc_kwh"],
        max_soc_kwh=bc["max_soc_kwh"],
        pv_dc_coupled=bc["pv_dc_coupled"],
        pv_dc_peak_power_kwp=bc["pv_dc_peak_power_kwp"],
        pv_dc_efficiency=bc["pv_dc_efficiency"],
    )

    sched = diag["data"]["optimization"]["schedule"]
    opt_data = diag["data"]["optimization"]
    options = diag["data"]["config_entry"]["options"]

    current_soc_kwh = diag["data"]["optimization"]["battery_state"]["soc_kwh"]
    price_forecast = sched["price_forecast"]
    pv_forecast = (
        sched["pv_forecast_kw"] if "pv_forecast_kw" in sched else sched["pv_forecast"]
    )
    consumption_forecast = (
        sched["consumption_forecast_kw"]
        if "consumption_forecast_kw" in sched
        else sched["consumption_forecast"]
    )
    step_durations_hours = sched.get("step_durations_hours")

    # step_start_times_iso lives in entity attributes, not in schedule dict
    step_start_times = sched.get("step_start_times_iso", [])
    if not step_start_times:
        for ent in diag.get("data", {}).get("entities", []):
            attrs = ent.get("attributes", {})
            if "step_start_times_iso" in attrs:
                step_start_times = attrs["step_start_times_iso"]
                break

    degradation_cost = options.get("degradation_cost_per_kwh", 0.04)
    min_price_spread = options.get("min_price_spread", 0.0)
    fixed_feed_in_price = options.get("fixed_feed_in_price", 0.04)

    return (
        battery,
        current_soc_kwh,
        price_forecast,
        pv_forecast,
        consumption_forecast,
        step_durations_hours,
        step_start_times,
        degradation_cost,
        min_price_spread,
        fixed_feed_in_price,
        opt_data,
        sched,
    )


# ---------------------------------------------------------------------------
# Inline DP optimizer (copy from optimizer.py, no HA imports)
# ---------------------------------------------------------------------------

SOC_RESOLUTION_WH = 25.0
POWER_STEP_W = 100
MIN_CYCLE_KWH = 0.2
POWER_IDLE_THRESHOLD_KW = 0.001
MIN_PV_SURPLUS_KW = 0.05
DC_TO_AC_INVERTER_EFFICIENCY = 0.96


def calculate_step_cost(
    time_step_hours,
    soc_wh,
    action_w,
    grid_price,
    feed_in_price,
    pv_production_w,
    consumption_w,
    rte,
    degradation_cost_per_kwh,
    battery_config,
    pv_dc_production_w=0.0,
):
    sqrt_rte = math.sqrt(rte)
    charge_eff = sqrt_rte
    discharge_eff = sqrt_rte
    dc_eff = (
        battery_config.pv_dc_efficiency if battery_config.pv_dc_coupled else sqrt_rte
    )

    pv_production_w = max(0.0, pv_production_w)
    pv_dc_production_w = max(0.0, pv_dc_production_w)

    dc_charge_w = 0.0
    ac_charge_w = 0.0
    dc_pv_excess_w = pv_dc_production_w
    throughput_kwh = 0.0

    if action_w > 0:
        dc_charge_w = min(action_w, pv_dc_production_w * dc_eff)
        ac_charge_w = action_w - dc_charge_w
        dc_pv_used_w = dc_charge_w / dc_eff if dc_eff > 0 else 0.0
        dc_pv_excess_w = max(0.0, pv_dc_production_w - dc_pv_used_w)
        grid_to_battery_w = ac_charge_w / charge_eff if ac_charge_w > 0 else 0.0
        throughput_kwh = action_w * time_step_hours / 1000
    elif action_w < 0:
        dc_pv_excess_w = pv_dc_production_w
        usable_power_w = abs(action_w) * discharge_eff
        grid_to_battery_w = -usable_power_w
        throughput_kwh = abs(action_w) * time_step_hours / 1000
    else:
        grid_to_battery_w = 0.0
        if battery_config.pv_dc_coupled and pv_dc_production_w > 0:
            max_soc_wh = battery_config.max_soc_kwh * 1000
            headroom_wh = max(0.0, max_soc_wh - soc_wh)
            passive_charge_wh = min(
                pv_dc_production_w * dc_eff * time_step_hours, headroom_wh
            )
            passive_charge_w = (
                passive_charge_wh / time_step_hours if time_step_hours > 0 else 0.0
            )
            dc_pv_consumed_w = passive_charge_w / dc_eff if dc_eff > 0 else 0.0
            dc_pv_excess_w = max(0.0, pv_dc_production_w - dc_pv_consumed_w)
            throughput_kwh = passive_charge_wh / 1000
        else:
            dc_pv_excess_w = pv_dc_production_w
            throughput_kwh = 0.0

    dc_pv_to_ac_w = (
        dc_pv_excess_w * DC_TO_AC_INVERTER_EFFICIENCY if dc_pv_excess_w > 0 else 0.0
    )
    total_ac_pv_w = pv_production_w + dc_pv_to_ac_w
    net_grid_w = consumption_w - total_ac_pv_w + grid_to_battery_w

    if battery_config.max_grid_power_kw > 0:
        cap_w = battery_config.max_grid_power_kw * 1000
        net_grid_w = max(-cap_w, min(cap_w, net_grid_w))

    energy_kwh = abs(net_grid_w) * time_step_hours / 1000
    if net_grid_w > 0:
        grid_cost = energy_kwh * grid_price
    else:
        grid_cost = -energy_kwh * feed_in_price

    degradation_cost = throughput_kwh * degradation_cost_per_kwh
    return grid_cost + degradation_cost


def _find_nearest_soc_idx(soc_wh, soc_states):
    if len(soc_states) <= 1:
        return 0
    step = soc_states[1] - soc_states[0]
    idx = round((soc_wh - soc_states[0]) / step)
    return max(0, min(idx, len(soc_states) - 1))


def run_dp(
    battery_config,
    current_soc_kwh,
    price_forecast,
    feed_in_forecast,
    pv_forecast,
    consumption_forecast,
    step_durations_hours,
    degradation_cost_per_kwh,
    min_price_spread,
    pv_dc_forecast=None,
):
    """Run the DP backward pass and return V, policy, soc_states, soc_resolution_wh."""
    if pv_dc_forecast is None:
        pv_dc_forecast = [0.0] * len(pv_forecast)

    n_steps = min(len(price_forecast), len(pv_forecast), len(consumption_forecast))
    if not step_durations_hours:
        step_durations_hours = [0.25] * n_steps
    elif len(step_durations_hours) < n_steps:
        step_durations_hours = list(step_durations_hours) + [
            step_durations_hours[-1]
        ] * (n_steps - len(step_durations_hours))

    min_step_hours = min(step_durations_hours[:n_steps])
    soc_resolution_wh = max(float(SOC_RESOLUTION_WH), POWER_STEP_W * min_step_hours)
    full_step_hours = (
        step_durations_hours[1] if len(step_durations_hours) > 1 else min_step_hours
    )
    aligned_step_w = soc_resolution_wh / full_step_hours
    power_step_w = max(float(POWER_STEP_W), aligned_step_w)

    min_soc_wh = round(battery_config.min_soc_kwh * 1000)
    max_soc_wh = round(battery_config.max_soc_kwh * 1000)

    n_soc_states = int(round((max_soc_wh - min_soc_wh) / soc_resolution_wh)) + 1
    soc_states = [min_soc_wh + i * soc_resolution_wh for i in range(n_soc_states)]

    INF = float("inf")
    V = [[INF] * n_soc_states for _ in range(n_steps + 1)]
    policy = [[0.0] * n_soc_states for _ in range(n_steps)]

    terminal_price = feed_in_forecast[-1] if feed_in_forecast else 0.0
    for s_idx, soc_wh in enumerate(soc_states):
        stored_kwh = (soc_wh - min_soc_wh) / 1000.0
        V[n_steps][s_idx] = -stored_kwh * terminal_price

    max_charge_w = battery_config.max_charge_power_kw * 1000
    max_discharge_w = battery_config.max_discharge_power_kw * 1000
    charge_steps = int(max_charge_w / power_step_w)
    charge_actions = [float(i * power_step_w) for i in range(charge_steps, -1, -1)]
    discharge_steps = int(max_discharge_w / power_step_w)
    discharge_actions = [
        float(-i * power_step_w) for i in range(discharge_steps, 0, -1)
    ]
    actions = discharge_actions + charge_actions

    for t in range(n_steps - 1, -1, -1):
        time_step_hours = step_durations_hours[t]
        grid_price = price_forecast[t]
        feed_in_price = feed_in_forecast[t] if t < len(feed_in_forecast) else grid_price
        pv_w = pv_forecast[t] * 1000 if t < len(pv_forecast) else 0
        pv_dc_w = pv_dc_forecast[t] * 1000 if t < len(pv_dc_forecast) else 0
        consumption_w = (
            consumption_forecast[t] * 1000 if t < len(consumption_forecast) else 0
        )

        for s_idx, soc_wh in enumerate(soc_states):
            best_cost = INF
            best_action = 0.0

            for action_w in actions:
                if action_w > 0:
                    energy_change_wh = action_w * time_step_hours
                    new_soc_wh = soc_wh + energy_change_wh
                    if new_soc_wh > max_soc_wh:
                        continue
                elif action_w < 0:
                    energy_change_wh = abs(action_w) * time_step_hours
                    new_soc_wh = soc_wh - energy_change_wh
                    if new_soc_wh < min_soc_wh:
                        continue
                else:
                    new_soc_wh = soc_wh

                new_soc_idx = _find_nearest_soc_idx(new_soc_wh, soc_states)
                if action_w != 0 and new_soc_idx == s_idx:
                    continue

                step_cost = calculate_step_cost(
                    time_step_hours=time_step_hours,
                    soc_wh=soc_wh,
                    action_w=action_w,
                    grid_price=grid_price,
                    feed_in_price=feed_in_price,
                    pv_production_w=pv_w,
                    consumption_w=consumption_w,
                    rte=battery_config.round_trip_efficiency,
                    degradation_cost_per_kwh=degradation_cost_per_kwh,
                    battery_config=battery_config,
                    pv_dc_production_w=pv_dc_w,
                )

                total_cost = step_cost + V[t + 1][new_soc_idx]
                if total_cost < best_cost:
                    best_cost = total_cost
                    best_action = action_w

            V[t][s_idx] = best_cost
            policy[t][s_idx] = best_action

    return (
        V,
        policy,
        soc_states,
        soc_resolution_wh,
        power_step_w,
        step_durations_hours,
        min_soc_wh,
        max_soc_wh,
        terminal_price,
    )


def forward_pass(
    policy,
    soc_states,
    current_soc_kwh,
    step_durations_hours,
    min_soc_wh,
    max_soc_wh,
    n_steps,
    battery_config,
    pv_dc_forecast,
):
    """Execute the forward pass to get the schedule."""
    current_soc = float(
        soc_states[_find_nearest_soc_idx(int(current_soc_kwh * 1000), soc_states)]
    )

    power_schedule_kw = []
    mode_schedule = []
    soc_schedule_kwh = [current_soc_kwh]  # Start with exact SoC

    for t in range(n_steps):
        time_step_hours = step_durations_hours[t]
        soc_idx = _find_nearest_soc_idx(current_soc, soc_states)
        action_w = policy[t][soc_idx]
        pv_dc_w = pv_dc_forecast[t] * 1000 if t < len(pv_dc_forecast) else 0.0

        power_kw = action_w / 1000
        power_schedule_kw.append(power_kw)

        if action_w > 0:
            mode_schedule.append("charging")
            current_soc = min(
                current_soc + action_w * time_step_hours, float(max_soc_wh)
            )
        elif action_w < 0:
            mode_schedule.append("discharging")
            current_soc = max(
                current_soc - abs(action_w) * time_step_hours, float(min_soc_wh)
            )
        else:
            if battery_config.pv_dc_coupled and pv_dc_w > 0:
                dc_eff = battery_config.pv_dc_efficiency
                headroom_wh = max(0.0, float(max_soc_wh) - current_soc)
                passive_wh = min(pv_dc_w * dc_eff * time_step_hours, headroom_wh)
                current_soc = current_soc + passive_wh
            mode_schedule.append("idle")

        soc_schedule_kwh.append(current_soc / 1000)

    return power_schedule_kw, mode_schedule, soc_schedule_kwh


# ---------------------------------------------------------------------------
# Analysis helpers
# ---------------------------------------------------------------------------


def profitability_analysis(
    price_forecast,
    feed_in_forecast,
    step_durations_hours,
    pv_forecast,
    consumption_forecast,
    battery_config,
    degradation_cost_per_kwh,
    terminal_price,
):
    """For each step, compute the profitability of charging/discharging."""
    sqrt_rte = math.sqrt(battery_config.round_trip_efficiency)
    rows = []

    for t, (price, feed_in, dt, pv, cons) in enumerate(
        zip(
            price_forecast,
            feed_in_forecast,
            step_durations_hours,
            pv_forecast,
            consumption_forecast,
        )
    ):
        pv_surplus = pv - cons
        effective_charge_price = feed_in if pv_surplus > MIN_PV_SURPLUS_KW else price

        # Net profit of charging 1 kWh into battery now (vs idle)
        # vs gaining terminal_price per kWh at end
        charge_grid_cost = effective_charge_price / sqrt_rte  # EUR per kWh stored
        charge_profit_vs_terminal = (
            terminal_price - charge_grid_cost - degradation_cost_per_kwh
        )

        # Net profit of discharging 1 kWh from battery (vs using terminal value)
        discharge_revenue = price * sqrt_rte  # EUR per kWh discharged
        discharge_profit_vs_terminal = (
            discharge_revenue - terminal_price - degradation_cost_per_kwh
        )

        # Break-even discharge price needed to beat terminal value
        breakeven_discharge = (terminal_price + degradation_cost_per_kwh) / sqrt_rte

        rows.append(
            {
                "t": t,
                "price": price,
                "feed_in": feed_in,
                "dt_h": dt,
                "pv_kw": pv,
                "cons_kw": cons,
                "eff_charge_price": effective_charge_price,
                "charge_profit": charge_profit_vs_terminal,  # >0 = charge worth it vs holding terminal value
                "discharge_profit": discharge_profit_vs_terminal,  # >0 = discharge worth it
                "breakeven_discharge": breakeven_discharge,
            }
        )

    return rows


def print_schedule(
    step_times,
    price_forecast,
    feed_in_forecast,
    pv_forecast,
    consumption_forecast,
    power_schedule_kw,
    mode_schedule,
    soc_schedule_kwh,
    step_durations_hours,
    terminal_price,
    degradation_cost,
    battery_config,
    recorded_power=None,
    recorded_mode=None,
    recorded_soc=None,
):
    """Print a detailed schedule table."""
    sqrt_rte = math.sqrt(battery_config.round_trip_efficiency)
    breakeven_discharge = (terminal_price + degradation_cost) / sqrt_rte

    print()
    print("=" * 110)
    print("  BATTERY SCHEDULE ANALYSIS")
    print(
        f"  RTE={battery_config.round_trip_efficiency:.2f}  √RTE={sqrt_rte:.4f}  "
        f"Cap={battery_config.max_soc_kwh:.2f}kWh  "
        f"Degradation={degradation_cost:.3f}€/kWh  "
        f"Terminal price={terminal_price:.4f}€/kWh"
    )
    print(
        f"  Break-even discharge price: {breakeven_discharge:.4f} €/kWh  "
        f"(need price > this to beat keeping energy for end-of-horizon)"
    )
    print("=" * 110)

    header = (
        f"{'Step':>4}  {'Time (UTC)':>19}  {'Price':>7}  {'Feed-in':>7}  "
        f"{'PV kW':>6}  {'Cons kW':>7}  {'Power kW':>8}  "
        f"{'Mode':>11}  {'SoC kWh':>8}  {'Disc profit':>11}  {'Chg profit':>10}"
    )
    if recorded_power is not None:
        header += f"  {'Rec.Power':>9}  {'Rec.Mode':>11}"

    print(header)
    print("-" * 110)

    n = min(len(price_forecast), len(power_schedule_kw))
    for t in range(n):
        price = price_forecast[t]
        feed_in = feed_in_forecast[t]
        pv = pv_forecast[t] if t < len(pv_forecast) else 0.0
        cons = consumption_forecast[t] if t < len(consumption_forecast) else 0.0
        power = power_schedule_kw[t]
        mode = mode_schedule[t]
        soc_before = soc_schedule_kwh[t]
        pv_surplus = pv - cons
        eff_charge_price = feed_in if pv_surplus > MIN_PV_SURPLUS_KW else price
        charge_profit = terminal_price - eff_charge_price / sqrt_rte - degradation_cost
        discharge_profit = price * sqrt_rte - terminal_price - degradation_cost

        # Mark interesting steps
        marker = ""
        if mode in ("charging", "discharging"):
            marker = "◄"
        elif discharge_profit > 0:
            marker = "?"  # Should be discharging but isn't
        elif charge_profit > 0:
            marker = (
                "+"  # Should be charging but isn't (neutral - may already be charging)
            )

        time_str = step_times[t] if t < len(step_times) else f"step {t:02d}"
        # Shorten timestamp "2026-03-12T12:43:55+00:00" → "03-12 12:43"
        if "T" in time_str:
            date_part = time_str[5:10]  # "03-12"
            time_part = time_str[11:16]  # "12:43"
            time_str = f"{date_part} {time_part}"

        row = (
            f"{t:>4}  {time_str:>19}  {price:>7.4f}  {feed_in:>7.4f}  "
            f"{pv:>6.3f}  {cons:>7.3f}  {power:>8.3f}  "
            f"{mode:>11}  {soc_before:>8.4f}  {discharge_profit:>+11.4f}  {charge_profit:>+10.4f}  {marker}"
        )
        if recorded_power is not None:
            rec_p = recorded_power[t] if t < len(recorded_power) else 0.0
            rec_m = recorded_mode[t] if t < len(recorded_mode) else "?"
            row += f"  {rec_p:>9.3f}  {rec_m:>11}"

        # Highlight charging/discharging steps
        if mode == "charging":
            row = "\033[32m" + row + "\033[0m"
        elif mode == "discharging":
            row = "\033[33m" + row + "\033[0m"
        elif discharge_profit > 0:
            row = "\033[31m" + row + "\033[0m"  # Red: missed discharge opportunity

        print(row)

    print("-" * 110)
    print(
        "  Columns: 'Disc profit' = price×√RTE - terminal_price - degradation per kWh discharged"
    )
    print(
        "           'Chg profit'  = terminal_price - price/√RTE - degradation per kWh charged"
    )
    print(
        "           Red rows: discharge would be profitable (profit > 0) but not scheduled"
    )
    print(
        "           Green: charging  |  Yellow: discharging  |  ?: unexplained missed discharge"
    )


def print_summary(
    battery,
    current_soc_kwh,
    terminal_price,
    degradation_cost,
    price_forecast,
    feed_in_forecast,
):
    sqrt_rte = math.sqrt(battery.round_trip_efficiency)
    breakeven_discharge = (terminal_price + degradation_cost) / sqrt_rte
    breakeven_charge = (
        terminal_price - degradation_cost
    ) * sqrt_rte  # max price to charge at

    print()
    print("=" * 70)
    print("  ECONOMIC SUMMARY")
    print("=" * 70)
    print(
        f"  Battery capacity:         {battery.max_soc_kwh:.3f} kWh  (min SoC {battery.min_soc_kwh:.3f} kWh)"
    )
    print(
        f"  Current SoC:              {current_soc_kwh:.4f} kWh  ({current_soc_kwh / battery.max_soc_kwh * 100:.1f}%)"
    )
    print(
        f"  RTE:                      {battery.round_trip_efficiency:.2f}  (√RTE = {sqrt_rte:.4f})"
    )
    print(f"  Degradation cost:         {degradation_cost:.4f} €/kWh")
    print()
    print(f"  Terminal price (last step): {terminal_price:.4f} €/kWh")
    print(
        f"    → Break-even discharge:   {breakeven_discharge:.4f} €/kWh  (need sell price > this)"
    )
    print(
        f"    → Break-even charge:      {breakeven_charge:.4f} €/kWh  (need buy price < this)"
    )
    print()

    max_price = max(price_forecast)
    min_price = min(price_forecast)
    max_idx = price_forecast.index(max_price)
    min_idx = price_forecast.index(min_price)
    print(f"  Price range in forecast:  {min_price:.4f} - {max_price:.4f} €/kWh")
    print(f"    min at step {min_idx:2d}:          {min_price:.4f} €/kWh")
    print(f"    max at step {max_idx:2d}:          {max_price:.4f} €/kWh")
    print()

    discharge_profitable = [
        p
        for p in price_forecast
        if p * sqrt_rte - terminal_price - degradation_cost > 0
    ]
    charge_profitable = [
        p
        for p in price_forecast
        if terminal_price - p / sqrt_rte - degradation_cost > 0
    ]
    print(f"  Steps where discharge is profitable: {len(discharge_profitable)}")
    print(f"  Steps where charging is profitable:  {len(charge_profitable)}")
    print()

    if max_price < breakeven_discharge:
        print(
            f"  ⚠  NO discharge is profitable: max price {max_price:.4f} < breakeven {breakeven_discharge:.4f}"
        )
        print(
            f"     Root cause: terminal_price ({terminal_price:.4f}) too high relative to forecast prices."
        )
        print(
            f"     The optimizer values stored energy at {terminal_price:.4f} €/kWh (end-of-horizon)"
        )
        print(
            f"     and discharging at even {max_price:.4f} €/kWh doesn't overcome that + degradation."
        )
    print("=" * 70)


def print_whatif(
    battery,
    price_forecast,
    feed_in_forecast,
    pv_forecast,
    consumption_forecast,
    step_durations_hours,
    current_soc_kwh,
    degradation_cost,
    min_price_spread,
    terminal_price,
):
    """Show what would happen with different terminal price assumptions."""
    sqrt_rte = math.sqrt(battery.round_trip_efficiency)
    max_price = max(price_forecast)

    print()
    print("=" * 70)
    print("  WHAT-IF: EFFECT OF TERMINAL PRICE ON DISCHARGE DECISIONS")
    print("=" * 70)
    print(
        f"  {'Terminal price':>15}  {'Breakeven discharge':>19}  {'Max forecast price':>18}  {'Discharge possible?':>19}"
    )
    print(f"  {'-' * 15}  {'-' * 19}  {'-' * 18}  {'-' * 19}")

    for tp in [
        0.04,
        0.07,
        0.10,
        terminal_price,
        terminal_price * 0.8,
        terminal_price * 0.6,
        (max_price * sqrt_rte - degradation_cost) * 0.99,  # just profitable
    ]:
        tp = round(tp, 4)
        breakeven = (tp + degradation_cost) / sqrt_rte
        possible = "YES ✓" if max_price >= breakeven else "NO ✗"
        marker = " ← actual" if abs(tp - terminal_price) < 1e-6 else ""
        print(
            f"  {tp:>15.4f}  {breakeven:>19.4f}  {max_price:>18.4f}  {possible:>19}{marker}"
        )

    print()
    print(
        "  The actual terminal price comes from feed_in_forecast[-1] = price_forecast[-1]"
    )
    print(f"  = last step's buy/sell price = {terminal_price:.4f} €/kWh")
    print(
        "  If the horizon ended at a low-price night period, the DP would discharge more."
    )
    print("=" * 70)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "diagnostics.json"
    diag = load_diagnostics(path)

    (
        battery,
        current_soc_kwh,
        price_forecast,
        pv_forecast,
        consumption_forecast,
        step_durations_hours,
        step_start_times,
        degradation_cost,
        min_price_spread,
        fixed_feed_in_price,
        opt_data,
        sched,
    ) = extract_inputs(diag)

    # The optimizer uses the same array for feed_in as for price when no
    # separate feed_in forecast is available. The coordinator may provide a
    # dynamic feed_in sensor value, but the schedule only stores price_forecast.
    # Use price_forecast as feed_in_forecast (matches actual DP behavior).
    feed_in_forecast = list(price_forecast)

    n_steps = len(price_forecast)
    pv_dc_forecast = [0.0] * n_steps

    print(f"\nLoaded {path}")
    print(
        f"  {n_steps} steps, SoC={current_soc_kwh:.4f} kWh, "
        f"degradation={degradation_cost:.3f} €/kWh, "
        f"min_spread={min_price_spread:.3f} €/kWh"
    )

    # Run DP
    (
        V,
        policy,
        soc_states,
        soc_resolution_wh,
        power_step_w,
        step_durations_hours,
        min_soc_wh,
        max_soc_wh,
        terminal_price,
    ) = run_dp(
        battery_config=battery,
        current_soc_kwh=current_soc_kwh,
        price_forecast=price_forecast,
        feed_in_forecast=feed_in_forecast,
        pv_forecast=pv_forecast,
        consumption_forecast=consumption_forecast,
        step_durations_hours=step_durations_hours,
        degradation_cost_per_kwh=degradation_cost,
        min_price_spread=min_price_spread,
        pv_dc_forecast=pv_dc_forecast,
    )

    print(
        f"  SoC resolution={soc_resolution_wh:.2f} Wh, "
        f"power step={power_step_w:.0f} W, "
        f"SoC states={len(soc_states)}, "
        f"terminal price={terminal_price:.4f} €/kWh"
    )

    # Forward pass
    power_schedule_kw, mode_schedule, soc_schedule_kwh = forward_pass(
        policy=policy,
        soc_states=soc_states,
        current_soc_kwh=current_soc_kwh,
        step_durations_hours=step_durations_hours,
        min_soc_wh=min_soc_wh,
        max_soc_wh=max_soc_wh,
        n_steps=n_steps,
        battery_config=battery,
        pv_dc_forecast=pv_dc_forecast,
    )

    # Recorded schedule from diagnostics
    rec_power = sched.get("power_schedule_kw")
    rec_mode = sched.get("mode_schedule")
    rec_soc = sched.get("soc_schedule_kwh")

    print_summary(
        battery,
        current_soc_kwh,
        terminal_price,
        degradation_cost,
        price_forecast,
        feed_in_forecast,
    )

    print_schedule(
        step_times=step_start_times,
        price_forecast=price_forecast,
        feed_in_forecast=feed_in_forecast,
        pv_forecast=pv_forecast,
        consumption_forecast=consumption_forecast,
        power_schedule_kw=power_schedule_kw,
        mode_schedule=mode_schedule,
        soc_schedule_kwh=soc_schedule_kwh,
        step_durations_hours=step_durations_hours,
        terminal_price=terminal_price,
        degradation_cost=degradation_cost,
        battery_config=battery,
        recorded_power=rec_power,
        recorded_mode=rec_mode,
        recorded_soc=rec_soc,
    )

    print_whatif(
        battery=battery,
        price_forecast=price_forecast,
        feed_in_forecast=feed_in_forecast,
        pv_forecast=pv_forecast,
        consumption_forecast=consumption_forecast,
        step_durations_hours=step_durations_hours,
        current_soc_kwh=current_soc_kwh,
        degradation_cost=degradation_cost,
        min_price_spread=min_price_spread,
        terminal_price=terminal_price,
    )


if __name__ == "__main__":
    main()

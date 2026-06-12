#!/usr/bin/env python3
"""
Battery schedule simulator: loads diagnostics.json and re-runs the optimizer,
then explains each scheduling decision step by step.

Usage:
    python simulate_diagnostics.py [diagnostics.json]

Helps diagnose why the optimizer made certain charge/discharge decisions.
If diagnostics include `control_action`, that published controller target can be
compared with the raw optimizer schedule.
"""

import json
import math
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
    high_soc_charge_threshold_pct: float = 100.0
    high_soc_max_charge_kw: float = 0.0
    low_soc_discharge_threshold_pct: float = 0.0
    low_soc_max_discharge_kw: float = 0.0

    def max_charge_at_soc(self, soc_kwh: float) -> float:
        if (
            self.high_soc_max_charge_kw > 0
            and soc_kwh / self.capacity_kwh * 100 >= self.high_soc_charge_threshold_pct
        ):
            return self.high_soc_max_charge_kw
        return self.max_charge_power_kw

    def max_discharge_at_soc(self, soc_kwh: float) -> float:
        if (
            self.low_soc_max_discharge_kw > 0
            and soc_kwh / self.capacity_kwh * 100
            <= self.low_soc_discharge_threshold_pct
        ):
            return self.low_soc_max_discharge_kw
        return self.max_discharge_power_kw


# ---------------------------------------------------------------------------
# Load diagnostics
# ---------------------------------------------------------------------------


def load_diagnostics(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def get_charge_eff_override(
    rte: float, charge_eff_correction: float | None
) -> float | None:
    """Return corrected charge-side efficiency when diagnostics contain a calibration."""
    if charge_eff_correction is None or charge_eff_correction >= 0.995:
        return None
    return math.sqrt(rte) * charge_eff_correction


def get_discharge_eff_override(
    rte: float, discharge_eff_correction: float | None
) -> float | None:
    """Return corrected discharge-side efficiency when diagnostics contain a calibration."""
    if discharge_eff_correction is None or discharge_eff_correction >= 0.995:
        return None
    # SoC transition: soc -= power * hours / discharge_eff.
    # To reduce planned SoC drop by factor `correction`, divide sqrt(RTE) by it
    # (a larger divisor yields a smaller SoC drop). May be > 1; only used for
    # SoC state transitions, not for economic cost.
    return math.sqrt(rte) / discharge_eff_correction


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
        high_soc_charge_threshold_pct=bc.get("high_soc_charge_threshold_pct", 100.0),
        high_soc_max_charge_kw=bc.get("high_soc_max_charge_kw", 0.0),
        low_soc_discharge_threshold_pct=bc.get("low_soc_discharge_threshold_pct", 0.0),
        low_soc_max_discharge_kw=bc.get("low_soc_max_discharge_kw", 0.0),
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

    # step_start_times_iso and step_durations_hours live in entity attributes, not in schedule dict
    step_start_times = sched.get("step_start_times_iso", [])
    if not step_start_times or not step_durations_hours:
        for ent in diag.get("data", {}).get("entities", []):
            attrs = ent.get("attributes", {})
            if not step_start_times and "step_start_times_iso" in attrs:
                step_start_times = attrs["step_start_times_iso"]
            if not step_durations_hours and "step_durations_hours" in attrs:
                step_durations_hours = attrs["step_durations_hours"]
            if step_start_times and step_durations_hours:
                break

    degradation_cost_per_cycle = options.get(
        "degradation_cost_per_cycle",
        options.get("degradation_cost_per_kwh", 0.04),  # fallback for old diagnostics
    )
    # Convert per-cycle to per-kWh for the DP (same as coordinator).
    # A full cycle moves usable capacity through the battery twice (charge +
    # discharge) and degradation is charged on throughput in both directions,
    # hence the division by 2 × usable_kwh.
    usable_kwh = battery.max_soc_kwh - battery.min_soc_kwh
    degradation_cost = (
        degradation_cost_per_cycle / (2 * usable_kwh)
        if usable_kwh > 0
        else degradation_cost_per_cycle
    )
    min_price_spread = options.get("min_price_spread", 0.0)
    fixed_feed_in_price = options.get("fixed_feed_in_price", 0.04)

    # Use actual feed-in forecast from schedule if available (new diagnostics field)
    feed_in_forecast = sched.get("feed_in_price_forecast") or list(price_forecast)

    # Terminal shadow price stored in schedule for DP reproduction
    terminal_shadow_price = sched.get("terminal_shadow_price")
    charge_eff_correction = opt_data.get("charge_eff_correction")
    if charge_eff_correction is None:
        run_log = opt_data.get("optimizer_run_log") or []
        if run_log:
            charge_eff_correction = run_log[-1].get("charge_eff_correction")

    discharge_eff_correction = opt_data.get("discharge_eff_correction")
    if discharge_eff_correction is None:
        run_log = opt_data.get("optimizer_run_log") or []
        if run_log:
            discharge_eff_correction = run_log[-1].get("discharge_eff_correction")

    return (
        battery,
        current_soc_kwh,
        price_forecast,
        feed_in_forecast,
        pv_forecast,
        consumption_forecast,
        step_durations_hours,
        step_start_times,
        degradation_cost,
        min_price_spread,
        fixed_feed_in_price,
        terminal_shadow_price,
        charge_eff_correction,
        discharge_eff_correction,
        opt_data,
        sched,
    )


# ---------------------------------------------------------------------------
# Inline DP optimizer (copy from optimizer.py, no HA imports)
# ---------------------------------------------------------------------------

SOC_RESOLUTION_WH = 10.0
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

    dc_pv_excess_w = pv_dc_production_w
    throughput_kwh = 0.0

    if action_w > 0:
        # AC charging: grid draws the AC setpoint; passive DC MPPT charging
        # continues on top, limited by the remaining headroom.
        grid_to_battery_w = action_w  # grid draws AC setpoint power
        ac_stored_wh = action_w * time_step_hours * charge_eff
        throughput_kwh = ac_stored_wh / 1000  # actual stored Wh via AC
        if battery_config.pv_dc_coupled and pv_dc_production_w > 0:
            max_soc_wh = battery_config.max_soc_kwh * 1000
            headroom_wh = max(0.0, max_soc_wh - soc_wh - ac_stored_wh)
            passive_charge_wh = min(
                pv_dc_production_w * dc_eff * time_step_hours, headroom_wh
            )
            passive_charge_w = (
                passive_charge_wh / time_step_hours if time_step_hours > 0 else 0.0
            )
            dc_pv_consumed_w = passive_charge_w / dc_eff if dc_eff > 0 else 0.0
            dc_pv_excess_w = max(0.0, pv_dc_production_w - dc_pv_consumed_w)
            throughput_kwh += passive_charge_wh / 1000
    elif action_w < 0:
        dc_pv_excess_w = pv_dc_production_w
        usable_power_w = abs(action_w)  # AC output = discharge setpoint
        grid_to_battery_w = -usable_power_w
        throughput_kwh = (
            abs(action_w) * time_step_hours / discharge_eff / 1000
        )  # actual battery-drawn Wh
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
    charge_eff_override=None,
    discharge_eff_override=None,
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
    soc_resolution_wh = float(SOC_RESOLUTION_WH)
    full_step_hours = (
        step_durations_hours[1] if len(step_durations_hours) > 1 else min_step_hours
    )
    aligned_step_w = soc_resolution_wh / full_step_hours
    power_step_w = max(float(POWER_STEP_W), aligned_step_w)

    min_soc_wh = round(battery_config.min_soc_kwh * 1000)
    max_soc_wh = round(battery_config.max_soc_kwh * 1000)
    sqrt_rte = math.sqrt(battery_config.round_trip_efficiency)
    charge_eff = charge_eff_override if charge_eff_override is not None else sqrt_rte
    discharge_eff = (
        discharge_eff_override if discharge_eff_override is not None else sqrt_rte
    )

    n_soc_states = int(round((max_soc_wh - min_soc_wh) / soc_resolution_wh)) + 1
    soc_states = [min_soc_wh + i * soc_resolution_wh for i in range(n_soc_states)]

    INF = float("inf")
    V = [[INF] * n_soc_states for _ in range(n_steps + 1)]
    policy = [[0.0] * n_soc_states for _ in range(n_steps)]

    if feed_in_forecast:
        lookback = max(1, min(round(6.0 / full_step_hours), len(feed_in_forecast)))
        sorted_prices = sorted(feed_in_forecast)
        median_price = sorted_prices[len(sorted_prices) // 2]
        clipped_tail = [min(p, median_price) for p in feed_in_forecast[-lookback:]]
        avg_tail = sum(clipped_tail) / len(clipped_tail)
        # Clamp at 0: negative feed-in tail must not penalize stored energy.
        terminal_price = max(0.0, min(feed_in_forecast[-1], avg_tail))
    else:
        terminal_price = 0.0
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

    soc_max_charge_w = [
        battery_config.max_charge_at_soc(s_wh / 1000) * 1000 for s_wh in soc_states
    ]
    soc_max_discharge_w = [
        battery_config.max_discharge_at_soc(s_wh / 1000) * 1000 for s_wh in soc_states
    ]

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
            max_chg_w = soc_max_charge_w[s_idx]
            max_dis_w = soc_max_discharge_w[s_idx]

            for action_w in actions:
                if action_w > 0:
                    if action_w > max_chg_w:
                        continue
                    energy_change_wh = action_w * time_step_hours * charge_eff
                    new_soc_wh = soc_wh + energy_change_wh
                    if new_soc_wh > max_soc_wh:
                        continue
                    if battery_config.pv_dc_coupled and pv_dc_w > 0:
                        dc_eff = battery_config.pv_dc_efficiency
                        headroom_wh = max(0.0, max_soc_wh - new_soc_wh)
                        new_soc_wh += min(
                            pv_dc_w * dc_eff * time_step_hours, headroom_wh
                        )
                elif action_w < 0:
                    if -action_w > max_dis_w:
                        continue
                    energy_change_wh = abs(action_w) * time_step_hours / discharge_eff
                    new_soc_wh = soc_wh - energy_change_wh
                    if new_soc_wh < min_soc_wh:
                        continue
                else:
                    if battery_config.pv_dc_coupled and pv_dc_w > 0:
                        dc_eff = battery_config.pv_dc_efficiency
                        headroom_wh = max(0.0, max_soc_wh - soc_wh)
                        passive_wh = min(
                            pv_dc_w * dc_eff * time_step_hours, headroom_wh
                        )
                        new_soc_wh = soc_wh + passive_wh
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

            if soc_wh > min_soc_wh:
                drain_w = (soc_wh - min_soc_wh) * discharge_eff / time_step_hours
                if 0 < drain_w <= max_dis_w:
                    step_cost = calculate_step_cost(
                        time_step_hours=time_step_hours,
                        soc_wh=soc_wh,
                        action_w=-drain_w,
                        grid_price=grid_price,
                        feed_in_price=feed_in_price,
                        pv_production_w=pv_w,
                        consumption_w=consumption_w,
                        rte=battery_config.round_trip_efficiency,
                        degradation_cost_per_kwh=degradation_cost_per_kwh,
                        battery_config=battery_config,
                        pv_dc_production_w=pv_dc_w,
                    )
                    total_cost = step_cost + V[t + 1][0]
                    if total_cost < best_cost:
                        best_cost = total_cost
                        best_action = -drain_w
            if soc_wh < max_soc_wh:
                fill_w = (max_soc_wh - soc_wh) / (time_step_hours * charge_eff)
                if 0 < fill_w <= max_chg_w:
                    step_cost = calculate_step_cost(
                        time_step_hours=time_step_hours,
                        soc_wh=soc_wh,
                        action_w=fill_w,
                        grid_price=grid_price,
                        feed_in_price=feed_in_price,
                        pv_production_w=pv_w,
                        consumption_w=consumption_w,
                        rte=battery_config.round_trip_efficiency,
                        degradation_cost_per_kwh=degradation_cost_per_kwh,
                        battery_config=battery_config,
                        pv_dc_production_w=pv_dc_w,
                    )
                    total_cost = step_cost + V[t + 1][n_soc_states - 1]
                    if total_cost < best_cost:
                        best_cost = total_cost
                        best_action = fill_w

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
    V,
    soc_states,
    current_soc_kwh,
    step_durations_hours,
    min_soc_wh,
    max_soc_wh,
    n_steps,
    battery_config,
    pv_dc_forecast,
    price_forecast,
    feed_in_forecast,
    pv_forecast,
    consumption_forecast,
    degradation_cost_per_kwh,
    power_step_w,
    charge_eff_override=None,
    discharge_eff_override=None,
):
    """Execute the forward pass via V-table re-evaluation at the actual continuous SoC."""
    sqrt_rte = math.sqrt(battery_config.round_trip_efficiency)
    charge_eff = charge_eff_override if charge_eff_override is not None else sqrt_rte
    discharge_eff = (
        discharge_eff_override if discharge_eff_override is not None else sqrt_rte
    )
    n_soc_states = len(soc_states)

    max_charge_w = battery_config.max_charge_power_kw * 1000
    max_discharge_w = battery_config.max_discharge_power_kw * 1000
    charge_steps = int(max_charge_w / power_step_w)
    discharge_steps = int(max_discharge_w / power_step_w)
    charge_actions = [float(i * power_step_w) for i in range(charge_steps, -1, -1)]
    discharge_actions = [
        float(-i * power_step_w) for i in range(discharge_steps, 0, -1)
    ]
    actions = discharge_actions + charge_actions

    INF = float("inf")
    current_soc = current_soc_kwh * 1000.0  # continuous, not snapped

    power_schedule_kw = []
    mode_schedule = []
    soc_schedule_kwh = [current_soc_kwh]

    for t in range(n_steps):
        time_step_hours = step_durations_hours[t]
        grid_price = price_forecast[t] if t < len(price_forecast) else 0.0
        feed_in_price = feed_in_forecast[t] if t < len(feed_in_forecast) else grid_price
        pv_w = pv_forecast[t] * 1000 if t < len(pv_forecast) else 0.0
        pv_dc_w = pv_dc_forecast[t] * 1000 if t < len(pv_dc_forecast) else 0.0
        consumption_w = (
            consumption_forecast[t] * 1000 if t < len(consumption_forecast) else 0.0
        )

        soc_idx = _find_nearest_soc_idx(current_soc, soc_states)
        max_chg_w = battery_config.max_charge_at_soc(current_soc / 1000) * 1000
        max_dis_w = battery_config.max_discharge_at_soc(current_soc / 1000) * 1000

        best_cost = INF
        best_action = 0.0
        best_new_soc = current_soc

        for action_w in actions:
            if action_w > 0:
                if action_w > max_chg_w:
                    continue
                new_soc_wh = current_soc + action_w * time_step_hours * charge_eff
                if new_soc_wh > max_soc_wh:
                    continue
                if battery_config.pv_dc_coupled and pv_dc_w > 0:
                    dc_eff = battery_config.pv_dc_efficiency
                    headroom_wh = max(0.0, float(max_soc_wh) - new_soc_wh)
                    new_soc_wh += min(pv_dc_w * dc_eff * time_step_hours, headroom_wh)
            elif action_w < 0:
                if -action_w > max_dis_w:
                    continue
                new_soc_wh = (
                    current_soc - abs(action_w) * time_step_hours / discharge_eff
                )
                if new_soc_wh < min_soc_wh:
                    continue
            else:
                if battery_config.pv_dc_coupled and pv_dc_w > 0:
                    dc_eff = battery_config.pv_dc_efficiency
                    headroom_wh = max(0.0, float(max_soc_wh) - current_soc)
                    passive_wh = min(pv_dc_w * dc_eff * time_step_hours, headroom_wh)
                    new_soc_wh = current_soc + passive_wh
                else:
                    new_soc_wh = current_soc

            new_soc_idx = _find_nearest_soc_idx(new_soc_wh, soc_states)
            if action_w != 0 and new_soc_idx == soc_idx:
                continue

            step_cost = calculate_step_cost(
                time_step_hours=time_step_hours,
                soc_wh=current_soc,
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
                best_new_soc = new_soc_wh

        # Boundary actions: exact power to drain to min or fill to max SoC
        if current_soc > min_soc_wh:
            drain_w = (current_soc - min_soc_wh) * discharge_eff / time_step_hours
            if 0 < drain_w <= max_dis_w:
                step_cost = calculate_step_cost(
                    time_step_hours=time_step_hours,
                    soc_wh=current_soc,
                    action_w=-drain_w,
                    grid_price=grid_price,
                    feed_in_price=feed_in_price,
                    pv_production_w=pv_w,
                    consumption_w=consumption_w,
                    rte=battery_config.round_trip_efficiency,
                    degradation_cost_per_kwh=degradation_cost_per_kwh,
                    battery_config=battery_config,
                    pv_dc_production_w=pv_dc_w,
                )
                total_cost = step_cost + V[t + 1][0]
                if total_cost < best_cost:
                    best_cost = total_cost
                    best_action = -drain_w
                    best_new_soc = float(min_soc_wh)

        if current_soc < max_soc_wh:
            fill_w = (max_soc_wh - current_soc) / (time_step_hours * charge_eff)
            if 0 < fill_w <= max_chg_w:
                step_cost = calculate_step_cost(
                    time_step_hours=time_step_hours,
                    soc_wh=current_soc,
                    action_w=fill_w,
                    grid_price=grid_price,
                    feed_in_price=feed_in_price,
                    pv_production_w=pv_w,
                    consumption_w=consumption_w,
                    rte=battery_config.round_trip_efficiency,
                    degradation_cost_per_kwh=degradation_cost_per_kwh,
                    battery_config=battery_config,
                    pv_dc_production_w=pv_dc_w,
                )
                total_cost = step_cost + V[t + 1][n_soc_states - 1]
                if total_cost < best_cost:
                    best_cost = total_cost
                    best_action = fill_w
                    best_new_soc = float(max_soc_wh)

        power_schedule_kw.append(best_action / 1000)
        if best_action > 0:
            mode_schedule.append("charging")
        elif best_action < 0:
            mode_schedule.append("discharging")
        else:
            mode_schedule.append("idle")
        current_soc = best_new_soc
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
    usable_kwh_disp = battery.max_soc_kwh - battery.min_soc_kwh
    print(
        f"  Degradation cost:         {degradation_cost:.4f} €/kWh"
        f"  ({degradation_cost * usable_kwh_disp:.4f} €/cycle, usable={usable_kwh_disp:.3f} kWh)"
    )
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
        "  The actual terminal price comes from the shadow price (λ) of the previous run"
    )
    print(
        "  when available, otherwise from min(feed_in_forecast[-1], 6-step tail average)"
    )
    print(f"  = {terminal_price:.4f} €/kWh")
    print(
        "  If the horizon ended at a low-price night period, the DP would discharge more."
    )
    print("=" * 70)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def print_min_spread_analysis(battery, degradation_cost, price_forecast):
    """Calculate and print the minimum price spread needed for profitable charge/discharge."""
    rte = battery.round_trip_efficiency
    sqrt_rte = math.sqrt(rte)
    min_p = min(price_forecast)
    max_p = max(price_forecast)
    actual_spread = max_p - min_p

    rte_loss = min_p * (1 / rte - 1)
    deg_loss = 2 * degradation_cost / sqrt_rte
    delta_min = rte_loss + deg_loss

    print()
    print("=" * 65)
    print("  MINIMUM SPREAD ANALYSIS")
    print("=" * 65)
    print(f"  RTE:              {rte:.2f}  (√RTE = {sqrt_rte:.4f})")
    print(f"  Degradation:      {degradation_cost:.2f} €/kWh")
    print(
        f"  Capacity:         {battery.max_soc_kwh} kWh  (usable: {battery.max_soc_kwh - battery.min_soc_kwh:.3f} kWh)"
    )
    print()
    print(f"  Price range forecast:  {min_p:.4f} – {max_p:.4f} €/kWh")
    print(f"  Actual spread:         {actual_spread * 100:.2f} ct/kWh")
    print()
    print(f"  Minimum spread needed (charge @ cheapest price {min_p:.4f}):")
    print(
        f"    RTE loss:          {rte_loss * 100:.2f} ct/kWh  (energy lost per cycle)"
    )
    print(
        f"    Degradation cost:  {deg_loss * 100:.2f} ct/kWh  (2 × {degradation_cost:.2f} / √RTE)"
    )
    print("    ─────────────────────────────")
    print(f"    Total Δ_min:       {delta_min * 100:.2f} ct/kWh")
    print()

    if actual_spread >= delta_min:
        margin = (actual_spread - delta_min) * 100
        usable = battery.max_soc_kwh - battery.min_soc_kwh
        max_profit = (actual_spread - delta_min) * usable
        print(
            f"  ✓ PROFITABLE: spread {actual_spread * 100:.2f} ct > minimum {delta_min * 100:.2f} ct  (margin: {margin:.2f} ct)"
        )
        print(
            f"    Max profit per cycle: {max_profit * 100:.2f} ct  ({max_profit:.4f} €)"
        )
    else:
        shortage = (delta_min - actual_spread) * 100
        print(
            f"  ✗ NOT PROFITABLE: spread {actual_spread * 100:.2f} ct < minimum {delta_min * 100:.2f} ct"
        )
        print(
            f"    Shortfall: {shortage:.2f} ct/kWh — optimizer correctly does not charge/discharge"
        )

    print()
    print("  Break-even spread per buy price:")
    print(
        f"  {'p_buy':>8}  {'Δ_min':>10}  {'p_sell_min':>10}  {'Spread achievable?':>18}"
    )
    for p in sorted({min_p, 0.10, 0.15, 0.20, 0.25, max_p}):
        d = p * (1 / rte - 1) + 2 * degradation_cost / sqrt_rte
        ok = "✓" if max_p >= p + d else "✗"
        print(f"  {p:>8.4f}  {d * 100:>9.2f}ct  {p + d:>10.4f}  {ok}")

    print()
    print("  Effect of higher RTE (at current prices):")
    for test_rte in [0.76, 0.80, 0.85, 0.90, 0.95]:
        sr = math.sqrt(test_rte)
        d = min_p * (1 / test_rte - 1) + 2 * degradation_cost / sr
        ok = (
            "✓ profitable"
            if actual_spread >= d
            else f"✗ shortfall {(d - actual_spread) * 100:.2f} ct"
        )
        marker = " ← current" if test_rte == rte else ""
        print(f"    RTE={test_rte:.2f}: Δ_min={d * 100:.2f} ct  {ok}{marker}")
    print("=" * 65)


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Battery controller diagnostic simulator"
    )
    parser.add_argument(
        "path", nargs="?", default="diagnostics.json", help="Diagnostics JSON file"
    )
    parser.add_argument(
        "--pv-curtailed",
        action="store_true",
        help="Simulate PV curtailment: zero out all PV forecasts before running the DP",
    )
    args = parser.parse_args()

    path = args.path
    diag = load_diagnostics(path)

    (
        battery,
        current_soc_kwh,
        price_forecast,
        feed_in_forecast,
        pv_forecast,
        consumption_forecast,
        step_durations_hours,
        step_start_times,
        degradation_cost,
        min_price_spread,
        fixed_feed_in_price,
        terminal_shadow_price,
        charge_eff_correction,
        discharge_eff_correction,
        opt_data,
        sched,
    ) = extract_inputs(diag)

    n_steps = len(price_forecast)
    pv_dc_forecast = [0.0] * n_steps

    # PV curtailment override (mirrors the coordinator's PV Curtailed switch)
    if args.pv_curtailed:
        pv_forecast = [0.0] * n_steps
        pv_dc_forecast = [0.0] * n_steps

    charge_eff_override = get_charge_eff_override(
        battery.round_trip_efficiency, charge_eff_correction
    )
    discharge_eff_override = get_discharge_eff_override(
        battery.round_trip_efficiency, discharge_eff_correction
    )

    print(f"\nLoaded {path}" + (" [PV CURTAILED]" if args.pv_curtailed else ""))
    print(
        f"  {n_steps} steps, SoC={current_soc_kwh:.4f} kWh, "
        f"degradation={degradation_cost:.3f} €/kWh, "
        f"min_spread={min_price_spread:.3f} €/kWh"
    )
    if terminal_shadow_price is not None:
        # Informational only: the DP terminal condition uses the feed-in tail.
        print(f"  shadow price (from diagnostics): {terminal_shadow_price:.4f} €/kWh")

    # Show charge/discharge efficiency calibration state if present in diagnostics
    opt_top = diag.get("data", diag).get("optimization", {})
    _charge_eff_correction = opt_top.get("charge_eff_correction")
    charge_eff_samples = opt_top.get("charge_eff_samples")
    if _charge_eff_correction is not None:
        flag = (
            " ⚠ correction active"
            if _charge_eff_correction < 0.995
            else " ✓ uncorrected"
        )
        samples_str = (
            f", n={charge_eff_samples} samples"
            if charge_eff_samples is not None
            else ""
        )
        print(
            f"  charge_eff_correction: {_charge_eff_correction:.4f}{samples_str}{flag}"
        )
    _discharge_eff_correction = opt_top.get("discharge_eff_correction")
    discharge_eff_samples = opt_top.get("discharge_eff_samples")
    if _discharge_eff_correction is not None:
        flag = (
            " ⚠ correction active"
            if _discharge_eff_correction < 0.995
            else " ✓ uncorrected"
        )
        samples_str = (
            f", n={discharge_eff_samples} samples"
            if discharge_eff_samples is not None
            else ""
        )
        print(
            f"  discharge_eff_correction: {_discharge_eff_correction:.4f}{samples_str}{flag}"
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
        charge_eff_override=charge_eff_override,
        discharge_eff_override=discharge_eff_override,
    )

    print(
        f"  SoC resolution={soc_resolution_wh:.2f} Wh, "
        f"power step={power_step_w:.0f} W, "
        f"SoC states={len(soc_states)}, "
        f"terminal price={terminal_price:.4f} €/kWh"
    )

    # Forward pass
    power_schedule_kw, mode_schedule, soc_schedule_kwh = forward_pass(
        V=V,
        soc_states=soc_states,
        current_soc_kwh=current_soc_kwh,
        step_durations_hours=step_durations_hours,
        min_soc_wh=min_soc_wh,
        max_soc_wh=max_soc_wh,
        n_steps=n_steps,
        battery_config=battery,
        pv_dc_forecast=pv_dc_forecast,
        price_forecast=price_forecast,
        feed_in_forecast=feed_in_forecast,
        pv_forecast=pv_forecast,
        consumption_forecast=consumption_forecast,
        degradation_cost_per_kwh=degradation_cost,
        power_step_w=power_step_w,
        charge_eff_override=charge_eff_override,
        discharge_eff_override=discharge_eff_override,
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

    # Raw vs processed cost comparison from diagnostics
    raw_total = opt_data.get("raw_total_cost")
    raw_sav = opt_data.get("raw_savings")
    proc_total = opt_data.get("total_cost")
    proc_sav = opt_data.get("savings")
    if raw_total is not None and proc_total is not None:
        print("\n  ── Raw vs processed costs (from diagnostics) ──")
        print(f"  Raw DP total cost:     {raw_total:.5f} €")
        print(f"  Processed total cost:  {proc_total:.5f} €")
        print(f"  Δ cost (filter impact): {proc_total - raw_total:+.5f} €")
        if raw_sav is not None and proc_sav is not None:
            print(f"  Raw savings:           {raw_sav:.5f} €")
            print(f"  Processed savings:     {proc_sav:.5f} €")
            print(f"  Δ savings:             {proc_sav - raw_sav:+.5f} €")

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

    print_min_spread_analysis(
        battery=battery,
        degradation_cost=degradation_cost,
        price_forecast=price_forecast,
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

    # ── Diagnostic history (only present in diagnostics from new versions)
    opt_data_top = diag.get("data", {}).get("optimization", {})
    run_log = opt_data_top.get("optimizer_run_log", [])
    setpoint_log = opt_data_top.get("setpoint_log", [])

    if run_log:
        print()
        print("=" * 120)
        print("  OPTIMIZER RUN HISTORY  (last run = most recent)")
        print("=" * 120)
        print(
            f"  {'Timestamp':>22}  {'Ctrl':>14}  {'DP mode':>11}  {'DP kW':>6}  "
            f"{'Eff.mode':>11}  {'Eff.kW':>6}  {'Setpt kW':>8}  "
            f"{'SoC kWh':>7}  {'Price':>7}  {'Shadow':>7}  {'Commit':>6}  {'Reason'}"
        )
        print("-" * 120)
        for entry in run_log:
            ts = entry.get("timestamp", "")[:19].replace("T", " ")
            locked = "YES" if entry.get("commitment_locked") else "no"
            reason = entry.get("commitment_reason", "")
            dev = ""
            dp_kw = entry.get("dp_power_kw", 0.0)
            eff_kw = entry.get("effective_power_kw", 0.0)
            setp_kw = entry.get("setpoint_kw", 0.0)
            if abs(setp_kw - eff_kw) > 0.05:
                dev = " ← SoC/power limit"
            elif abs(eff_kw - dp_kw) > 0.05 and not entry.get("commitment_locked"):
                dev = " ← mode override"
            print(
                f"  {ts:>22}  {entry.get('control_mode', ''):>14}  "
                f"{entry.get('dp_mode', ''):>11}  {dp_kw:>6.3f}  "
                f"{entry.get('effective_mode', ''):>11}  {eff_kw:>6.3f}  {setp_kw:>8.3f}  "
                f"{entry.get('soc_kwh', 0):>7.3f}  "
                f"{entry.get('current_price') or 0:>7.4f}  "
                f"{entry.get('shadow_price_eur_kwh') or 0:>7.4f}  "
                f"{locked:>6}  {reason}{dev}"
            )
        print("-" * 120)

    if setpoint_log:
        print()
        print("=" * 110)
        print("  REAL-TIME SETPOINT HISTORY  (last entry = most recent)")
        print("=" * 110)
        print(
            f"  {'Timestamp':>22}  {'Sched kW':>8}  {'Setp kW':>7}  {'Raw kW':>6}  "
            f"{'Mode':>14}  {'Eff.mode':>14}  {'SoC kWh':>7}  "
            f"{'Grid kW':>7}  {'Batt kW':>7}  {'SoC lim'}"
        )
        print("-" * 110)
        for entry in setpoint_log:
            ts = entry.get("timestamp", "")[:19].replace("T", " ")
            soc_lim = "YES" if entry.get("soc_limited") else "no"
            sched = entry.get("schedule_kw", 0.0)
            setp = entry.get("setpoint_kw", 0.0)
            raw = entry.get("raw_target_kw", 0.0)
            row = (
                f"  {ts:>22}  {sched:>8.3f}  {setp:>7.3f}  {raw:>6.3f}  "
                f"{entry.get('mode', ''):>14}  {entry.get('effective_mode', ''):>14}  "
                f"{entry.get('soc_kwh', 0):>7.3f}  "
                f"{entry.get('grid_kw', 0):>7.3f}  {entry.get('battery_kw', 0):>7.3f}  {soc_lim}"
            )
            if entry.get("soc_limited"):
                row = "\033[31m" + row + "\033[0m"
            elif abs(setp - sched) > 0.1:
                row = "\033[33m" + row + "\033[0m"
            print(row)
        print("-" * 110)
        print(
            "  Red = SoC limit blocked setpoint  |  Yellow = setpoint deviates from schedule"
        )


if __name__ == "__main__":
    main()

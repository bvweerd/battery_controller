"""Dynamic Programming optimizer for battery scheduling."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from .battery_model import BatteryConfig
from .const import (
    DC_TO_AC_INVERTER_EFFICIENCY,
    MIN_CYCLE_KWH,
    MIN_PV_SURPLUS_KW,
    POWER_IDLE_THRESHOLD_KW,
    POWER_STEP_W,
    SOC_RESOLUTION_WH,
)

_LOGGER = logging.getLogger(__name__)


@dataclass
class OptimizationResult:
    """Result of battery optimization."""

    # Schedule
    power_schedule_kw: list[float]  # Positive = charge, negative = discharge
    mode_schedule: list[str]  # 'charging', 'discharging', 'idle'
    soc_schedule_kwh: list[float]  # Expected SoC at each step

    # Costs
    total_cost: float  # Total cost over planning horizon
    baseline_cost: float  # Cost without battery
    savings: float  # Savings from optimization

    # Current step recommendation
    optimal_power_kw: float
    optimal_mode: str

    # Shadow price: marginal value of 1 kWh stored right now (EUR/kWh).
    # Represents how much future costs decrease per additional kWh in the battery.
    # Use as a threshold: charge when buy_price < shadow_price / sqrt(RTE),
    # and discharge/export when feed_in_price > shadow_price * sqrt(RTE).
    shadow_price_eur_kwh: float

    # Metadata
    price_forecast: list[float]
    pv_forecast: list[float]
    consumption_forecast: list[float]


def calculate_step_cost(
    time_step_hours: float,
    soc_wh: float,
    action_w: float,  # positive = charge, negative = discharge
    grid_price: float,  # EUR/kWh buy price
    feed_in_price: float,  # EUR/kWh sell price
    pv_production_w: float,  # AC-side PV production in W
    consumption_w: float,
    rte: float,  # Round Trip Efficiency
    degradation_cost_per_kwh: float,  # EUR/kWh throughput
    battery_config: BatteryConfig,
    pv_dc_production_w: float = 0.0,  # DC-coupled PV production in W
    charge_eff_override: float | None = None,  # Override sqrt(RTE) for charging
    discharge_eff_override: float | None = None,  # Override sqrt(RTE) for discharging
) -> float:
    """Calculate cost for a single time step.

    Cost calculation with RTE, degradation, and DC-coupled PV:

    1. RTE Effect (AC path):
       - charge_efficiency = sqrt(RTE) ~ 0.95 for RTE=0.90
       - discharge_efficiency = sqrt(RTE) ~ 0.95
       - Charging: grid_energy = battery_energy / charge_eff
       - Discharging: usable_energy = battery_energy * discharge_eff
       - When overrides are provided (from calculate_efficiency), these include
         C-rate and SoC derating on top of the base sqrt(RTE).

    2. DC-coupled PV:
       - PV panels connected directly to battery inverter DC bus
       - Charge efficiency ~97% (MPPT only, no AC conversion)
       - This PV power is "free" and doesn't pass through grid meter
       - In idle mode: DC PV passively charges battery up to available headroom
       - Excess DC PV (when battery full or discharging) goes through inverter to AC

    3. Degradation:
       - Every kWh through the battery costs degradation
       - DC PV charging also counts for degradation
       - Prevents unnecessary cycles at small price differences

    4. Grid capacity cap:
       - If battery_config.max_grid_power_kw > 0, both import and export are capped.
       - Excess PV that cannot be exported is treated as zero-revenue generation.

    Args:
        time_step_hours: Duration of time step in hours
        soc_wh: Current state of charge in Wh
        action_w: Battery action in W (positive = charge, negative = discharge)
        grid_price: Grid buy price in EUR/kWh
        feed_in_price: Grid sell price in EUR/kWh
        pv_production_w: AC-side PV production in W (already inverted, clamped >= 0)
        consumption_w: Consumption in W
        rte: Round trip efficiency (0-1)
        degradation_cost_per_kwh: Degradation cost in EUR/kWh throughput
        battery_config: Battery configuration
        pv_dc_production_w: DC-coupled PV production in W (before inverter, clamped >= 0)
        charge_eff_override: Actual charge efficiency including C-rate/SoC derating.
        discharge_eff_override: Actual discharge efficiency including C-rate/SoC derating.

    Returns:
        Total cost in EUR for this time step
    """
    sqrt_rte = math.sqrt(rte)
    # Use overridden efficiencies if provided (include C-rate/SoC derating)
    charge_eff = charge_eff_override if charge_eff_override is not None else sqrt_rte
    discharge_eff = (
        discharge_eff_override if discharge_eff_override is not None else sqrt_rte
    )
    dc_eff = (
        battery_config.pv_dc_efficiency if battery_config.pv_dc_coupled else sqrt_rte
    )

    # Clamp PV values: a faulty sensor should not appear as load
    pv_production_w = max(0.0, pv_production_w)
    pv_dc_production_w = max(0.0, pv_dc_production_w)

    # Handle DC-coupled PV
    # DC PV can charge battery directly at higher efficiency.
    # In idle mode (action_w == 0) DC-coupled inverters still absorb DC PV into
    # the battery up to available headroom — the AC setpoint of 0 only stops
    # AC-side grid charging, not DC MPPT charging.
    dc_charge_w = 0.0
    ac_charge_w = 0.0
    dc_pv_excess_w = pv_dc_production_w  # DC PV not used by battery -> goes to AC
    throughput_kwh = 0.0

    if action_w > 0:  # CHARGING
        # Use DC PV first (free energy, higher efficiency)
        dc_charge_w = min(action_w, pv_dc_production_w * dc_eff)
        ac_charge_w = action_w - dc_charge_w

        # DC PV not used by battery goes to AC side (through inverter)
        dc_pv_used_w = dc_charge_w / dc_eff if dc_eff > 0 else 0.0
        dc_pv_excess_w = max(0.0, pv_dc_production_w - dc_pv_used_w)

        # AC charging needs grid energy (with AC charge efficiency losses)
        grid_to_battery_w = ac_charge_w / charge_eff if ac_charge_w > 0 else 0.0

        throughput_kwh = action_w * time_step_hours / 1000

    elif action_w < 0:  # DISCHARGING
        # All DC PV excess goes to AC side when discharging
        dc_pv_excess_w = pv_dc_production_w

        # Energy from battery to home (including discharge losses)
        usable_power_w = abs(action_w) * discharge_eff
        grid_to_battery_w = -usable_power_w  # Negative = to home

        throughput_kwh = abs(action_w) * time_step_hours / 1000

    else:  # IDLE (action_w == 0)
        grid_to_battery_w = 0.0
        if battery_config.pv_dc_coupled and pv_dc_production_w > 0:
            # DC-coupled inverters absorb DC PV into the battery passively even
            # when the AC setpoint is 0. Model this as free passive charging up
            # to the available SoC headroom.
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

    # DC PV excess converted to AC (through inverter, ~96% efficiency)
    dc_pv_to_ac_w = (
        dc_pv_excess_w * DC_TO_AC_INVERTER_EFFICIENCY if dc_pv_excess_w > 0 else 0.0
    )

    # Total AC-side PV = external AC PV + DC PV excess converted to AC
    total_ac_pv_w = pv_production_w + dc_pv_to_ac_w

    # Net grid exchange (positive = buy, negative = sell)
    net_grid_w = consumption_w - total_ac_pv_w + grid_to_battery_w

    # Apply grid capacity cap: limit both import and export (0 = unlimited)
    if battery_config.max_grid_power_kw > 0:
        cap_w = battery_config.max_grid_power_kw * 1000
        net_grid_w = max(-cap_w, min(cap_w, net_grid_w))

    # Grid costs/revenue
    energy_kwh = abs(net_grid_w) * time_step_hours / 1000
    if net_grid_w > 0:
        grid_cost = energy_kwh * grid_price  # Buying
    else:
        grid_cost = -energy_kwh * feed_in_price  # Selling (negative cost)

    # Degradation costs (all battery throughput, including passive DC PV charging)
    degradation_cost = throughput_kwh * degradation_cost_per_kwh

    return grid_cost + degradation_cost


def optimize_battery_schedule(
    battery_config: BatteryConfig,
    current_soc_kwh: float,
    price_forecast: list[float],  # EUR/kWh buy prices
    feed_in_forecast: list[float] | None,  # EUR/kWh sell prices (optional)
    pv_forecast: list[float],  # kW (AC-side PV)
    consumption_forecast: list[float],  # kW
    step_durations_hours: list[float] | None = None,  # per-step duration in hours
    degradation_cost_per_kwh: float = 0.03,
    min_price_spread: float = 0.05,
    pv_dc_forecast: list[float] | None = None,  # kW (DC-coupled PV)
    terminal_shadow_price: float | None = None,  # EUR/kWh from previous run
) -> OptimizationResult:
    """Optimize battery schedule using dynamic programming.

    Uses backward induction to find optimal charge/discharge schedule.

    Args:
        battery_config: Battery configuration
        current_soc_kwh: Current state of charge in kWh
        price_forecast: Grid buy price forecast in EUR/kWh
        feed_in_forecast: Grid sell price forecast in EUR/kWh (optional)
        pv_forecast: AC-side PV production forecast in kW
        consumption_forecast: Consumption forecast in kW
        step_durations_hours: Duration of each time step in hours. When aligned
            to price-sensor boundaries, step 0 is the partial remainder of the
            current price period; all subsequent steps are full intervals.
            Defaults to 0.25 h (15 min) for every step when None.
        degradation_cost_per_kwh: Degradation cost in EUR/kWh
        min_price_spread: Minimum price spread for arbitrage
        pv_dc_forecast: DC-coupled PV production forecast in kW (optional)
        terminal_shadow_price: Shadow price (λ) from the previous optimization
            run, used as the terminal condition value for stored energy.  When
            provided this replaces the feed-in tail average, producing a more
            stable rolling-horizon schedule because λ is derived from the full
            price structure rather than a single end-of-horizon price point.
            Must be ≥ 0; negative values are ignored (fallback to feed-in tail).

    Returns:
        OptimizationResult with optimal schedule
    """
    # Use buy price as feed-in price if not provided
    if feed_in_forecast is None:
        feed_in_forecast = price_forecast

    # Default DC PV forecast to zeros if not provided
    if pv_dc_forecast is None:
        pv_dc_forecast = [0.0] * len(pv_forecast)

    n_steps = min(len(price_forecast), len(pv_forecast), len(consumption_forecast))
    if n_steps == 0:
        return _empty_result(battery_config, current_soc_kwh)

    # Normalise step_durations_hours to exactly n_steps entries.
    if not step_durations_hours:
        step_durations_hours = [0.25] * n_steps
    elif len(step_durations_hours) < n_steps:
        step_durations_hours = list(step_durations_hours) + [
            step_durations_hours[-1]
        ] * (n_steps - len(step_durations_hours))

    sqrt_rte = math.sqrt(battery_config.round_trip_efficiency)

    # Discretize SoC space.
    # Use the *minimum* step duration only for SoC resolution (to ensure that
    # even the shortest step moves the SoC by at least one state boundary).
    min_step_hours = min(step_durations_hours[:n_steps])

    # Resolution is the larger of SOC_RESOLUTION_WH and one power-step's energy
    # over the shortest step — accounting for charge/discharge efficiency (sqrt_rte)
    # since SoC transitions now use action_w × dt × sqrt_rte.  Without the RTE
    # factor the resolution was over-estimated (e.g. 55 Wh instead of 48 Wh for
    # a 0.55 h partial first step at RTE=0.76), causing unnecessary coarseness.
    soc_resolution_wh = max(
        float(SOC_RESOLUTION_WH), POWER_STEP_W * min_step_hours * sqrt_rte
    )

    # Align the power step to the SoC resolution using the *full* interval step,
    # not min_step_hours.  The first step is typically a short partial interval
    # (e.g. 3 min before the next price boundary); using its duration would inflate
    # power_step_w and restrict the available actions for ALL subsequent full-interval
    # steps (e.g. 1200 W max becomes 750 W when min_step=4 min with hourly prices).
    # The per-action sub-resolution check (new_soc_idx == s_idx) already handles
    # the short first step: actions that don't cross a state boundary are skipped.
    full_step_hours = (
        step_durations_hours[1] if len(step_durations_hours) > 1 else min_step_hours
    )
    aligned_step_w = soc_resolution_wh / full_step_hours
    power_step_w = max(float(POWER_STEP_W), aligned_step_w)
    # Round to nearest Wh to avoid floating-point drift (e.g. 2.12 * 0.1 * 1000
    # = 212.00000000000003).  Without rounding, soc_states[0] may be computed as
    # 912.0 (losing the tiny fractional part) while min_soc_wh stays at
    # 212.00000000000003, so the action that would exactly reach min_soc passes
    # the boundary check (212.0 < 212.00000000000003) and gets skipped.
    min_soc_wh = round(battery_config.min_soc_kwh * 1000)
    max_soc_wh = round(battery_config.max_soc_kwh * 1000)

    n_soc_states = int(round((max_soc_wh - min_soc_wh) / soc_resolution_wh)) + 1
    soc_states = [min_soc_wh + i * soc_resolution_wh for i in range(n_soc_states)]

    # Initialize value function (cost-to-go)
    # V[t][s] = minimum cost from time t to end, starting at SoC state s
    INF = float("inf")
    V = [[INF] * n_soc_states for _ in range(n_steps + 1)]
    policy = [[0.0] * n_soc_states for _ in range(n_steps)]

    # Terminal condition: value of stored energy at end of horizon.
    # Energy above min_soc can be sold at (approximately) the last known
    # feed-in price. A non-zero terminal value prevents the optimizer from
    # irrationally discharging the battery just before the horizon ends.
    #
    # Preferred source: shadow price (λ) from the previous run.  λ is the
    # marginal value of stored energy derived from the full price structure,
    # so it is more stable across rolling-horizon runs than the spot price at
    # a single end-of-horizon time step.
    # Fallback: blend the last price with the 6-step tail average to dampen
    # artifacts caused by transient price spikes at the forecast boundary.
    if terminal_shadow_price is not None and terminal_shadow_price >= 0.0:
        terminal_price = terminal_shadow_price
    elif feed_in_forecast:
        lookback = min(6, len(feed_in_forecast))
        avg_tail = sum(feed_in_forecast[-lookback:]) / lookback
        terminal_price = min(feed_in_forecast[-1], avg_tail)
    else:
        terminal_price = 0.0
    for s_idx, soc_wh in enumerate(soc_states):
        stored_kwh = (soc_wh - min_soc_wh) / 1000.0
        V[n_steps][s_idx] = -stored_kwh * terminal_price

    # Power action space (discretized in W)
    max_charge_w = battery_config.max_charge_power_kw * 1000
    max_discharge_w = battery_config.max_discharge_power_kw * 1000

    # Generate actions up to (but never exceeding) the rated max power.
    # Using integer division ensures the last step stays within limits.
    # Charge actions are listed highest-first so that when multiple actions yield
    # equal total cost (e.g. same-price blocks with identical PV/consumption),
    # the DP's strict-less-than comparison naturally picks the highest power.
    # This produces front-loaded charging (full power first) instead of a ramp-up,
    # which is both more intuitive and more robust against forecast uncertainty.
    charge_steps = int(max_charge_w / power_step_w)
    charge_actions = [float(i * power_step_w) for i in range(charge_steps, -1, -1)]
    discharge_steps = int(max_discharge_w / power_step_w)
    discharge_actions = [
        float(-i * power_step_w) for i in range(discharge_steps, 0, -1)
    ]
    actions = discharge_actions + charge_actions

    # Backward induction
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
                # SoC transition: action_w is battery-side power (explicit AC command).
                # For DC-coupled systems in idle mode (action_w == 0), the MPPT
                # charger passively charges the battery from DC PV up to available
                # headroom — the AC setpoint of 0 only stops AC-side grid charging.
                # Efficiency losses are on the grid/AC side and handled in
                # calculate_step_cost.
                if action_w > 0:
                    energy_change_wh = action_w * time_step_hours * sqrt_rte
                    new_soc_wh = soc_wh + energy_change_wh
                    if new_soc_wh > max_soc_wh:
                        continue
                elif action_w < 0:
                    # Discharge: action_w is AC setpoint → battery must supply
                    # abs(action_w) / discharge_eff from its DC side.
                    energy_change_wh = abs(action_w) * time_step_hours / sqrt_rte
                    new_soc_wh = soc_wh - energy_change_wh
                    if new_soc_wh < min_soc_wh:
                        continue
                else:
                    # Idle: no explicit AC charge/discharge.
                    # For DC-coupled systems, DC PV passively charges the battery.
                    if battery_config.pv_dc_coupled and pv_dc_w > 0:
                        dc_eff = battery_config.pv_dc_efficiency
                        headroom_wh = max(0.0, max_soc_wh - soc_wh)
                        passive_wh = min(
                            pv_dc_w * dc_eff * time_step_hours, headroom_wh
                        )
                        new_soc_wh = soc_wh + passive_wh
                    else:
                        new_soc_wh = soc_wh

                # Find nearest SoC state for next step
                new_soc_idx = _find_nearest_soc_idx(new_soc_wh, soc_states)

                # Skip sub-resolution AC actions: a non-zero action that doesn't
                # cross a SoC state boundary appears "free" to the DP (no future
                # value change) while still incurring real RTE losses, producing
                # oscillating micro-charge/discharge artifacts.
                # Idle (action_w == 0) is exempt — passive DC PV may still change
                # the SoC bin, and even if it doesn't, the cost is real.
                if action_w != 0 and new_soc_idx == s_idx:
                    continue

                # Calculate immediate cost
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

                # Total cost = immediate + future
                total_cost = step_cost + V[t + 1][new_soc_idx]

                if total_cost < best_cost:
                    best_cost = total_cost
                    best_action = action_w

            V[t][s_idx] = best_cost
            policy[t][s_idx] = best_action

    # Find current SoC index in the DP state space (needed for forward pass).
    # Shadow price is computed after post-processing filters below.
    current_soc_wh = int(current_soc_kwh * 1000)
    current_soc_idx = _find_nearest_soc_idx(current_soc_wh, soc_states)

    # Forward pass: extract optimal schedule

    power_schedule_kw = []
    mode_schedule = []
    soc_schedule_kwh = [current_soc_kwh]

    current_soc = float(soc_states[current_soc_idx])

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
                current_soc + action_w * time_step_hours * sqrt_rte, float(max_soc_wh)
            )
        elif action_w < 0:
            mode_schedule.append("discharging")
            current_soc = max(
                current_soc - abs(action_w) * time_step_hours / sqrt_rte,
                float(min_soc_wh),
            )
        else:
            # Idle: account for passive DC PV charging in forward pass
            if battery_config.pv_dc_coupled and pv_dc_w > 0:
                dc_eff = battery_config.pv_dc_efficiency
                headroom_wh = max(0.0, float(max_soc_wh) - current_soc)
                passive_wh = min(pv_dc_w * dc_eff * time_step_hours, headroom_wh)
                current_soc = current_soc + passive_wh
            mode_schedule.append("idle")

        soc_schedule_kwh.append(current_soc / 1000)

    # Oscillation filter window: at least 2 h, but scale with battery size so
    # that a small battery (5 kWh / 5 kW = 1 h cycle) uses a shorter window and
    # a large battery (20 kWh / 5 kW = 4 h cycle) uses a longer one.
    usable_kwh = battery_config.max_soc_kwh - battery_config.min_soc_kwh
    cycle_hours = (
        usable_kwh / battery_config.max_discharge_power_kw
        if battery_config.max_discharge_power_kw > 0
        else 2.0
    )
    oscillation_window_hours = max(2.0, cycle_hours)

    # Post-process: remove unprofitable oscillations
    power_schedule_kw, mode_schedule, soc_schedule_kwh = _filter_oscillations(
        power_schedule_kw=power_schedule_kw,
        mode_schedule=mode_schedule,
        soc_schedule_kwh=soc_schedule_kwh,
        price_forecast=price_forecast[:n_steps],
        min_price_spread=min_price_spread,
        degradation_cost_per_kwh=degradation_cost_per_kwh,
        rte=battery_config.round_trip_efficiency,
        step_durations_hours=step_durations_hours[:n_steps],
        min_soc_kwh=battery_config.min_soc_kwh,
        max_soc_kwh=battery_config.max_soc_kwh,
        pv_forecast=pv_forecast[:n_steps],
        consumption_forecast=consumption_forecast[:n_steps],
        feed_in_forecast=(
            feed_in_forecast[:n_steps] if feed_in_forecast else price_forecast[:n_steps]
        ),
        oscillation_window_hours=oscillation_window_hours,
    )

    # Post-process: suppress micro-cycles (P5.1)
    power_schedule_kw, mode_schedule, soc_schedule_kwh = _filter_micro_cycles(
        power_schedule_kw=power_schedule_kw,
        mode_schedule=mode_schedule,
        soc_schedule_kwh=soc_schedule_kwh,
        step_durations_hours=step_durations_hours[:n_steps],
        min_cycle_kwh=MIN_CYCLE_KWH,
    )

    # Shadow price: marginal value of 1 kWh stored at t=0, current SoC.
    # Computed after post-processing filters so it is consistent with the
    # filtered schedule presented to the caller. V[0] is not modified by the
    # filters (backward-pass values are stable), but placing the computation
    # here makes the intent clear: shadow price belongs to the filtered world.
    #   λ = -dV/dSoC = (V[s-1] - V[s+1]) / (2 * ΔSoC_kwh)
    # V is cost (lower is better); more energy lowers cost → gradient negative
    # → shadow price positive.
    step_kwh = soc_resolution_wh / 1000.0
    shadow_price_eur_kwh = 0.0
    if n_soc_states >= 3 and 0 < current_soc_idx < n_soc_states - 1:
        shadow_price_eur_kwh = (
            V[0][current_soc_idx - 1] - V[0][current_soc_idx + 1]
        ) / (2 * step_kwh)
    elif n_soc_states >= 2:
        if current_soc_idx == 0:
            shadow_price_eur_kwh = (V[0][0] - V[0][1]) / step_kwh
        else:
            shadow_price_eur_kwh = (V[0][-2] - V[0][-1]) / step_kwh

    # Calculate costs
    total_cost = V[0][current_soc_idx]

    # Calculate baseline cost (no battery action)
    # Baseline: DC PV excess goes to AC via inverter, no battery buffering
    baseline_cost = 0.0
    for t in range(n_steps):
        time_step_hours = step_durations_hours[t]
        grid_price = price_forecast[t]
        feed_in_price = feed_in_forecast[t] if t < len(feed_in_forecast) else grid_price
        pv_w = pv_forecast[t] * 1000 if t < len(pv_forecast) else 0
        pv_dc_w = pv_dc_forecast[t] * 1000 if t < len(pv_dc_forecast) else 0
        consumption_w = (
            consumption_forecast[t] * 1000 if t < len(consumption_forecast) else 0
        )

        # Without battery: DC PV excess goes to AC (through inverter)
        dc_pv_to_ac_w = pv_dc_w * DC_TO_AC_INVERTER_EFFICIENCY if pv_dc_w > 0 else 0
        total_pv_w = pv_w + dc_pv_to_ac_w

        net_grid_w = consumption_w - total_pv_w
        energy_kwh = abs(net_grid_w) * time_step_hours / 1000

        if net_grid_w > 0:
            baseline_cost += energy_kwh * grid_price
        else:
            baseline_cost -= energy_kwh * feed_in_price

    # Savings = value added by battery ACTIONS only.
    # total_cost already contains the terminal value of stored energy at horizon end.
    # baseline_cost does not include any terminal value.
    # Subtracting the terminal value of the *initial* SoC makes savings = 0 when the
    # battery is idle, regardless of how much energy is already stored.
    initial_stored_kwh = max(0.0, (soc_states[current_soc_idx] - min_soc_wh) / 1000.0)
    initial_terminal_value = initial_stored_kwh * terminal_price
    savings = baseline_cost - initial_terminal_value - total_cost

    return OptimizationResult(
        power_schedule_kw=power_schedule_kw,
        mode_schedule=mode_schedule,
        soc_schedule_kwh=soc_schedule_kwh,
        total_cost=total_cost,
        baseline_cost=baseline_cost,
        savings=savings,
        optimal_power_kw=power_schedule_kw[0] if power_schedule_kw else 0.0,
        optimal_mode=mode_schedule[0] if mode_schedule else "idle",
        shadow_price_eur_kwh=shadow_price_eur_kwh,
        price_forecast=list(price_forecast[:n_steps]),
        pv_forecast=list(pv_forecast[:n_steps]),
        consumption_forecast=list(consumption_forecast[:n_steps]),
    )


def _filter_oscillations(
    power_schedule_kw: list[float],
    mode_schedule: list[str],
    soc_schedule_kwh: list[float],
    price_forecast: list[float],
    min_price_spread: float,
    degradation_cost_per_kwh: float,
    rte: float,
    step_durations_hours: list[float],
    min_soc_kwh: float,
    max_soc_kwh: float,
    pv_forecast: list[float] | None = None,
    consumption_forecast: list[float] | None = None,
    feed_in_forecast: list[float] | None = None,
    oscillation_window_hours: float = 2.0,
) -> tuple[list[float], list[str], list[float]]:
    """Filter out unprofitable oscillations from the schedule.

    Removes rapid charge/discharge switches that don't have sufficient
    price spread to justify the round-trip efficiency losses and degradation.

    Takes into account PV surplus opportunity cost (feed-in price) when
    evaluating charging profitability.

    Args:
        power_schedule_kw: Power schedule in kW
        mode_schedule: Mode schedule
        soc_schedule_kwh: SoC schedule in kWh
        price_forecast: Grid buy price forecast in EUR/kWh
        min_price_spread: Minimum price spread required
        degradation_cost_per_kwh: Degradation cost
        rte: Round trip efficiency
        step_durations_hours: Per-step duration in hours
        min_soc_kwh: Minimum SoC
        max_soc_kwh: Maximum SoC
        pv_forecast: PV production forecast in kW (optional)
        consumption_forecast: Consumption forecast in kW (optional)
        feed_in_forecast: Feed-in price forecast in EUR/kWh (optional)

    Returns:
        Filtered (power_schedule, mode_schedule, soc_schedule)
    """
    if len(power_schedule_kw) == 0:
        return power_schedule_kw, mode_schedule, soc_schedule_kwh

    sqrt_rte = math.sqrt(rte)
    filtered_power = list(power_schedule_kw)
    filtered_mode = list(mode_schedule)
    filtered_soc = list(soc_schedule_kwh)

    # Minimum profitable price spread needed for arbitrage
    # P_discharge * sqrt(rte) > P_charge / sqrt(rte) + 2 * degradation + min_spread
    # => P_discharge > P_charge / rte + (2 * degradation + min_spread) / sqrt(rte)
    min_arbitrage_spread = (2 * degradation_cost_per_kwh + min_price_spread) / sqrt_rte

    # Helper to get actual charge cost (grid price or feed-in opportunity cost)
    def get_charge_cost(timestep: int) -> float:
        """Get the actual cost of charging at a given timestep.

        If there's PV surplus, charging costs the feed-in opportunity cost.
        Otherwise, it costs the grid price.
        """
        if pv_forecast and consumption_forecast and feed_in_forecast:
            pv_surplus = pv_forecast[timestep] - consumption_forecast[timestep]
            if pv_surplus > MIN_PV_SURPLUS_KW:
                # Charging with PV surplus = opportunity cost of not selling
                return feed_in_forecast[timestep]
        # Otherwise charging from grid
        return price_forecast[timestep]

    # Lookahead window: use first step duration as representative interval.
    # The first step may be shorter (partial interval), making the window
    # slightly larger in step count — this is conservative and safe.
    ref_step_h = step_durations_hours[0] if step_durations_hours else 0.25
    lookahead_steps = max(1, round(oscillation_window_hours / ref_step_h))

    # Iterative scan: repeat until convergence so that suppressing one step
    # also triggers re-evaluation of any steps that depended on it (orphaned
    # discharge after paired charge is suppressed, and vice-versa).
    changed = True
    while changed:
        changed = False
        i = 0
        while i < len(filtered_mode) - 1:
            if filtered_mode[i] == "charging":
                # Look ahead for quick discharge
                for j in range(i + 1, min(i + lookahead_steps + 1, len(filtered_mode))):
                    if filtered_mode[j] == "discharging":
                        charge_cost = get_charge_cost(i)
                        discharge_price = price_forecast[j]
                        effective_spread = discharge_price - charge_cost / rte
                        if effective_spread < min_arbitrage_spread:
                            filtered_power[i] = 0.0
                            filtered_mode[i] = "idle"
                            changed = True
                        break
            elif filtered_mode[i] == "discharging":
                # Look ahead for quick charge
                for j in range(i + 1, min(i + lookahead_steps + 1, len(filtered_mode))):
                    if filtered_mode[j] == "charging":
                        discharge_price = price_forecast[i]
                        charge_cost = get_charge_cost(j)
                        effective_spread = discharge_price - charge_cost / rte
                        if effective_spread < min_arbitrage_spread:
                            filtered_power[i] = 0.0
                            filtered_mode[i] = "idle"
                            changed = True
                        break
            i += 1

    # Recalculate SoC schedule and update power values to match actual SoC changes
    current_soc_kwh = soc_schedule_kwh[0]
    filtered_soc = [current_soc_kwh]

    for t in range(len(filtered_power)):
        step_h = (
            step_durations_hours[t]
            if t < len(step_durations_hours)
            else step_durations_hours[-1]
        )
        power_kw = filtered_power[t]
        prev_soc = current_soc_kwh
        if power_kw > 0:  # Charging: SoC += AC_power × charge_eff × dt
            current_soc_kwh = min(
                current_soc_kwh + power_kw * step_h * sqrt_rte, max_soc_kwh
            )
        elif power_kw < 0:  # Discharging: SoC -= AC_power / discharge_eff × dt
            current_soc_kwh = max(
                current_soc_kwh - abs(power_kw) * step_h / sqrt_rte, min_soc_kwh
            )

        # Update power to match actual SoC change (e.g. if battery was full/empty).
        # Charging:   AC_power = delta_soc / (dt × charge_eff)
        # Discharging: AC_power = delta_soc × discharge_eff / dt  (delta_soc < 0)
        delta_soc = current_soc_kwh - prev_soc
        if delta_soc >= 0:
            actual_power_kw = delta_soc / (step_h * sqrt_rte) if step_h > 0 else 0.0
        else:
            actual_power_kw = delta_soc * sqrt_rte / step_h if step_h > 0 else 0.0
        filtered_power[t] = actual_power_kw

        # Update mode if power changed to 0
        if abs(actual_power_kw) < POWER_IDLE_THRESHOLD_KW:
            filtered_mode[t] = "idle"

        filtered_soc.append(current_soc_kwh)

    return filtered_power, filtered_mode, filtered_soc


def _filter_micro_cycles(
    power_schedule_kw: list[float],
    mode_schedule: list[str],
    soc_schedule_kwh: list[float],
    step_durations_hours: list[float],
    min_cycle_kwh: float = 0.2,
) -> tuple[list[float], list[str], list[float]]:
    """Filter out micro-cycles whose total energy is below min_cycle_kwh.

    Charge or discharge segments that move less energy than min_cycle_kwh have
    disproportionately high degradation cost per kWh of useful storage.  Replacing
    them with idle preserves battery lifespan without meaningful cost impact.

    Args:
        power_schedule_kw: Power schedule in kW
        mode_schedule: Mode schedule
        soc_schedule_kwh: SoC schedule in kWh
        step_durations_hours: Per-step duration in hours
        min_cycle_kwh: Minimum energy per charge/discharge block in kWh

    Returns:
        Filtered (power_schedule, mode_schedule, soc_schedule)
    """
    if not power_schedule_kw:
        return power_schedule_kw, mode_schedule, soc_schedule_kwh

    filtered_power = list(power_schedule_kw)
    filtered_mode = list(mode_schedule)

    i = 0
    while i < len(filtered_mode):
        current_dir = filtered_mode[i]
        if current_dir not in ("charging", "discharging"):
            i += 1
            continue

        # Find the extent of this contiguous charge/discharge block
        j = i
        total_energy_kwh = 0.0
        while j < len(filtered_mode) and filtered_mode[j] == current_dir:
            step_h = (
                step_durations_hours[j]
                if j < len(step_durations_hours)
                else step_durations_hours[-1]
            )
            total_energy_kwh += abs(filtered_power[j]) * step_h
            j += 1

        if total_energy_kwh < min_cycle_kwh:
            for k in range(i, j):
                filtered_power[k] = 0.0
                filtered_mode[k] = "idle"

        i = j

    return filtered_power, filtered_mode, soc_schedule_kwh


def _find_nearest_soc_idx(soc_wh: float, soc_states: list[float]) -> int:
    """Find the index of the nearest SoC state.

    Uses direct calculation since soc_states is a uniform grid,
    giving O(1) lookup instead of O(n) linear scan.
    """
    if len(soc_states) <= 1:
        return 0
    step = soc_states[1] - soc_states[0]
    idx = round((soc_wh - soc_states[0]) / step)
    return max(0, min(idx, len(soc_states) - 1))


def _empty_result(
    battery_config: BatteryConfig,
    current_soc_kwh: float,
) -> OptimizationResult:
    """Return an empty optimization result."""
    return OptimizationResult(
        power_schedule_kw=[],
        mode_schedule=[],
        soc_schedule_kwh=[current_soc_kwh],
        total_cost=0.0,
        baseline_cost=0.0,
        savings=0.0,
        optimal_power_kw=0.0,
        optimal_mode="idle",
        shadow_price_eur_kwh=0.0,
        price_forecast=[],
        pv_forecast=[],
        consumption_forecast=[],
    )

"""Dynamic Programming optimizer for battery scheduling."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from .battery_model import BatteryConfig
from .const import SOC_RESOLUTION_WH

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
) -> float:
    """Calculate cost for a single time step.

    Cost calculation with RTE, degradation, and DC-coupled PV:

    1. RTE Effect (AC path):
       - charge_efficiency = sqrt(RTE) ~ 0.95 for RTE=0.90
       - discharge_efficiency = sqrt(RTE) ~ 0.95
       - Charging: grid_energy = battery_energy / charge_eff
       - Discharging: usable_energy = battery_energy * discharge_eff

    2. DC-coupled PV:
       - PV panels connected directly to battery inverter DC bus
       - Charge efficiency ~97% (MPPT only, no AC conversion)
       - This PV power is "free" and doesn't pass through grid meter
       - Excess DC PV (when battery full) goes through inverter to AC

    3. Degradation:
       - Every kWh through the battery costs degradation
       - DC PV charging also counts for degradation
       - Prevents unnecessary cycles at small price differences

    Args:
        time_step_hours: Duration of time step in hours
        soc_wh: Current state of charge in Wh
        action_w: Battery action in W (positive = charge, negative = discharge)
        grid_price: Grid buy price in EUR/kWh
        feed_in_price: Grid sell price in EUR/kWh
        pv_production_w: AC-side PV production in W (already inverted)
        consumption_w: Consumption in W
        rte: Round trip efficiency (0-1)
        degradation_cost_per_kwh: Degradation cost in EUR/kWh throughput
        battery_config: Battery configuration
        pv_dc_production_w: DC-coupled PV production in W (before inverter)

    Returns:
        Total cost in EUR for this time step
    """
    sqrt_rte = math.sqrt(rte)
    dc_eff = (
        battery_config.pv_dc_efficiency if battery_config.pv_dc_coupled else sqrt_rte
    )

    # Handle DC-coupled PV
    # DC PV can charge battery directly at higher efficiency
    # Only relevant when charging (action_w > 0)
    dc_charge_w = 0.0
    ac_charge_w = 0.0
    dc_pv_excess_w = pv_dc_production_w  # DC PV not used by battery -> goes to AC

    if action_w > 0:  # CHARGING
        # Use DC PV first (free energy, higher efficiency)
        dc_charge_w = min(action_w, pv_dc_production_w * dc_eff)
        ac_charge_w = action_w - dc_charge_w

        # DC PV not used by battery goes to AC side (through inverter)
        dc_pv_used_w = dc_charge_w / dc_eff  # Actual DC PV consumed
        dc_pv_excess_w = max(0, pv_dc_production_w - dc_pv_used_w)

        # AC charging needs grid energy (with AC charge efficiency losses)
        grid_to_battery_w = ac_charge_w / sqrt_rte if ac_charge_w > 0 else 0.0
    else:  # DISCHARGING or IDLE
        # DC PV excess goes entirely to AC side (battery not absorbing it)
        dc_pv_excess_w = pv_dc_production_w

        if action_w < 0:
            # Energy from battery to home (including discharge losses)
            usable_power_w = abs(action_w) * sqrt_rte
            grid_to_battery_w = -usable_power_w  # Negative = to home
        else:
            grid_to_battery_w = 0.0

    # DC PV excess converted to AC (through inverter, ~96% efficiency)
    dc_pv_to_ac_w = dc_pv_excess_w * 0.96 if dc_pv_excess_w > 0 else 0.0

    # Total AC-side PV = external AC PV + DC PV excess converted to AC
    total_ac_pv_w = pv_production_w + dc_pv_to_ac_w

    # Net grid exchange (positive = buy, negative = sell)
    net_grid_w = consumption_w - total_ac_pv_w + grid_to_battery_w

    # Grid costs/revenue
    energy_kwh = abs(net_grid_w) * time_step_hours / 1000
    if net_grid_w > 0:
        grid_cost = energy_kwh * grid_price  # Buying
    else:
        grid_cost = -energy_kwh * feed_in_price  # Selling (negative cost)

    # Degradation costs (all battery throughput, including DC PV)
    throughput_kwh = abs(action_w) * time_step_hours / 1000
    degradation_cost = throughput_kwh * degradation_cost_per_kwh

    return grid_cost + degradation_cost


def optimize_battery_schedule(
    battery_config: BatteryConfig,
    current_soc_kwh: float,
    price_forecast: list[float],  # EUR/kWh buy prices
    feed_in_forecast: list[float] | None,  # EUR/kWh sell prices (optional)
    pv_forecast: list[float],  # kW (AC-side PV)
    consumption_forecast: list[float],  # kW
    time_step_minutes: int = 15,
    degradation_cost_per_kwh: float = 0.03,
    min_price_spread: float = 0.05,
    pv_dc_forecast: list[float] | None = None,  # kW (DC-coupled PV)
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
        time_step_minutes: Time step duration in minutes
        degradation_cost_per_kwh: Degradation cost in EUR/kWh
        min_price_spread: Minimum price spread for arbitrage
        pv_dc_forecast: DC-coupled PV production forecast in kW (optional)

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

    time_step_hours = time_step_minutes / 60.0

    # Discretize SoC space
    min_soc_wh = int(battery_config.min_soc_kwh * 1000)
    max_soc_wh = int(battery_config.max_soc_kwh * 1000)
    soc_states = list(
        range(min_soc_wh, max_soc_wh + SOC_RESOLUTION_WH, SOC_RESOLUTION_WH)
    )
    n_soc_states = len(soc_states)

    # Initialize value function (cost-to-go)
    # V[t][s] = minimum cost from time t to end, starting at SoC state s
    INF = float("inf")
    V = [[INF] * n_soc_states for _ in range(n_steps + 1)]
    policy = [[0.0] * n_soc_states for _ in range(n_steps)]

    # Terminal condition: value of stored energy at end of horizon.
    # Energy above min_soc can be sold at (approximately) the last known
    # feed-in price. A non-zero terminal value prevents the optimizer from
    # irrationally discharging the battery just before the horizon ends.
    terminal_price = feed_in_forecast[-1] if feed_in_forecast else 0.0
    for s_idx, soc_wh in enumerate(soc_states):
        stored_kwh = (soc_wh - min_soc_wh) / 1000.0
        V[n_steps][s_idx] = -stored_kwh * terminal_price

    # Power action space (discretized in W)
    max_charge_w = battery_config.max_charge_power_kw * 1000
    max_discharge_w = battery_config.max_discharge_power_kw * 1000
    power_step_w = 100  # 100W resolution

    # Generate actions up to (but never exceeding) the rated max power.
    # Using integer division ensures the last step stays within limits.
    charge_steps = int(max_charge_w / power_step_w)
    charge_actions = [float(i * power_step_w) for i in range(charge_steps + 1)]
    discharge_steps = int(max_discharge_w / power_step_w)
    discharge_actions = [
        float(-i * power_step_w) for i in range(discharge_steps, 0, -1)
    ]
    actions = discharge_actions + charge_actions

    # Backward induction
    for t in range(n_steps - 1, -1, -1):
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
                # SoC transition: action_w is battery-side power.
                # Efficiency losses are on the grid/AC side and handled
                # in calculate_step_cost. The battery stores/releases
                # exactly action_w * time_step_hours Wh.
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

                # Find nearest SoC state for next step
                new_soc_idx = _find_nearest_soc_idx(new_soc_wh, soc_states)

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

    # Shadow price: marginal value of 1 kWh stored at t=0, current SoC.
    # Computed as the numerical derivative of V[0] with respect to SoC:
    #   λ = -dV/dSoC = (V[s-1] - V[s+1]) / (2 * ΔSoC_kwh)
    # Because V is cost (lower is better) and more energy lowers cost,
    # the gradient is negative → shadow price is positive.
    current_soc_wh = int(current_soc_kwh * 1000)
    current_soc_idx = _find_nearest_soc_idx(current_soc_wh, soc_states)
    step_kwh = SOC_RESOLUTION_WH / 1000.0
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

    # Forward pass: extract optimal schedule

    power_schedule_kw = []
    mode_schedule = []
    soc_schedule_kwh = [current_soc_kwh]

    current_soc = float(soc_states[current_soc_idx])

    for t in range(n_steps):
        soc_idx = _find_nearest_soc_idx(current_soc, soc_states)
        action_w = policy[t][soc_idx]

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
            mode_schedule.append("idle")

        soc_schedule_kwh.append(current_soc / 1000)

    # Post-process: remove unprofitable oscillations
    power_schedule_kw, mode_schedule, soc_schedule_kwh = _filter_oscillations(
        power_schedule_kw=power_schedule_kw,
        mode_schedule=mode_schedule,
        soc_schedule_kwh=soc_schedule_kwh,
        price_forecast=price_forecast[:n_steps],
        min_price_spread=min_price_spread,
        degradation_cost_per_kwh=degradation_cost_per_kwh,
        rte=battery_config.round_trip_efficiency,
        time_step_hours=time_step_hours,
        min_soc_kwh=battery_config.min_soc_kwh,
        max_soc_kwh=battery_config.max_soc_kwh,
        pv_forecast=pv_forecast[:n_steps],
        consumption_forecast=consumption_forecast[:n_steps],
        feed_in_forecast=(
            feed_in_forecast[:n_steps] if feed_in_forecast else price_forecast[:n_steps]
        ),
    )

    # Calculate costs
    total_cost = V[0][current_soc_idx]

    # Calculate baseline cost (no battery action)
    # Baseline: DC PV excess goes to AC via inverter, no battery buffering
    baseline_cost = 0.0
    for t in range(n_steps):
        grid_price = price_forecast[t]
        feed_in_price = feed_in_forecast[t] if t < len(feed_in_forecast) else grid_price
        pv_w = pv_forecast[t] * 1000 if t < len(pv_forecast) else 0
        pv_dc_w = pv_dc_forecast[t] * 1000 if t < len(pv_dc_forecast) else 0
        consumption_w = (
            consumption_forecast[t] * 1000 if t < len(consumption_forecast) else 0
        )

        # Without battery: DC PV excess goes to AC (through inverter)
        dc_pv_to_ac_w = pv_dc_w * 0.96 if pv_dc_w > 0 else 0
        total_pv_w = pv_w + dc_pv_to_ac_w

        net_grid_w = consumption_w - total_pv_w
        energy_kwh = abs(net_grid_w) * time_step_hours / 1000

        if net_grid_w > 0:
            baseline_cost += energy_kwh * grid_price
        else:
            baseline_cost -= energy_kwh * feed_in_price

    savings = baseline_cost - total_cost

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
    time_step_hours: float,
    min_soc_kwh: float,
    max_soc_kwh: float,
    pv_forecast: list[float] | None = None,
    consumption_forecast: list[float] | None = None,
    feed_in_forecast: list[float] | None = None,
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
        time_step_hours: Time step duration in hours
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
            if pv_surplus > 0.05:  # 50W threshold for PV surplus
                # Charging with PV surplus = opportunity cost of not selling
                return feed_in_forecast[timestep]
        # Otherwise charging from grid
        return price_forecast[timestep]

    # Look for rapid charge/discharge oscillations
    i = 0
    while i < len(filtered_mode) - 1:
        if filtered_mode[i] == "charging":
            # Look ahead for quick discharge
            for j in range(i + 1, min(i + 8, len(filtered_mode))):  # 2 hours lookahead
                if filtered_mode[j] == "discharging":
                    # Found charge followed by discharge - check if profitable
                    charge_cost = get_charge_cost(i)  # May be feed-in opportunity cost
                    discharge_price = price_forecast[j]
                    effective_spread = discharge_price - charge_cost / rte

                    if effective_spread < min_arbitrage_spread:
                        # Not profitable - replace with idle
                        filtered_power[i] = 0.0
                        filtered_mode[i] = "idle"
                        break
        elif filtered_mode[i] == "discharging":
            # Look ahead for quick charge
            for j in range(i + 1, min(i + 8, len(filtered_mode))):
                if filtered_mode[j] == "charging":
                    # Found discharge followed by charge - check if profitable
                    discharge_price = price_forecast[i]
                    charge_cost = get_charge_cost(j)  # May be feed-in opportunity cost
                    effective_spread = discharge_price - charge_cost / rte

                    if effective_spread < min_arbitrage_spread:
                        # Not profitable - replace with idle
                        filtered_power[i] = 0.0
                        filtered_mode[i] = "idle"
                        break
        i += 1

    # Recalculate SoC schedule after filtering
    current_soc_kwh = soc_schedule_kwh[0]
    filtered_soc = [current_soc_kwh]

    for t in range(len(filtered_power)):
        power_kw = filtered_power[t]
        if power_kw > 0:  # Charging
            current_soc_kwh = min(
                current_soc_kwh + power_kw * time_step_hours, max_soc_kwh
            )
        elif power_kw < 0:  # Discharging
            current_soc_kwh = max(
                current_soc_kwh + power_kw * time_step_hours, min_soc_kwh
            )
        filtered_soc.append(current_soc_kwh)

    return filtered_power, filtered_mode, filtered_soc


def _find_nearest_soc_idx(soc_wh: float, soc_states: list[int]) -> int:
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

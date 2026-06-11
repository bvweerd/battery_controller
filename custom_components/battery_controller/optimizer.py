"""Dynamic Programming optimizer for battery scheduling."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from .battery_model import BatteryConfig
from .const import (
    ACTION_CHARGING,
    ACTION_DISCHARGING,
    ACTION_IDLE,
    DC_TO_AC_INVERTER_EFFICIENCY,
    MIN_CYCLE_KWH,
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
    raw_total_cost: float | None = None
    raw_savings: float | None = None


def _rebuild_schedule(
    power_schedule_kw: list[float],
    step_durations_hours: list[float],
    initial_soc_kwh: float,
    min_soc_kwh: float,
    max_soc_kwh: float,
    rte: float,
    pv_dc_forecast: list[float] | None = None,
    pv_dc_coupled: bool = False,
    pv_dc_efficiency: float = 0.97,
    discharge_eff: float | None = None,
    charge_eff: float | None = None,
) -> tuple[list[float], list[str], list[float]]:
    """Rebuild schedule after post-processing so SoC stays physically consistent."""
    sqrt_rte = math.sqrt(rte)
    _discharge_eff = discharge_eff if discharge_eff is not None else sqrt_rte
    _charge_eff = charge_eff if charge_eff is not None else sqrt_rte
    rebuilt_power = list(power_schedule_kw)
    rebuilt_mode: list[str] = []
    soc_schedule = [initial_soc_kwh]
    current_soc_kwh = initial_soc_kwh

    if pv_dc_forecast is None:
        pv_dc_forecast = [0.0] * len(power_schedule_kw)

    for t, commanded_power_kw in enumerate(rebuilt_power):
        step_h = (
            step_durations_hours[t]
            if t < len(step_durations_hours)
            else step_durations_hours[-1]
        )
        prev_soc_kwh = current_soc_kwh

        if commanded_power_kw > 0:
            current_soc_kwh = min(
                current_soc_kwh + commanded_power_kw * step_h * _charge_eff,
                max_soc_kwh,
            )
            delta_soc = current_soc_kwh - prev_soc_kwh
            actual_power_kw = delta_soc / (step_h * _charge_eff) if step_h > 0 else 0.0
        elif commanded_power_kw < 0:
            current_soc_kwh = max(
                current_soc_kwh - abs(commanded_power_kw) * step_h / _discharge_eff,
                min_soc_kwh,
            )
            delta_soc = current_soc_kwh - prev_soc_kwh
            actual_power_kw = delta_soc * _discharge_eff / step_h if step_h > 0 else 0.0
        else:
            if pv_dc_coupled and t < len(pv_dc_forecast) and pv_dc_forecast[t] > 0:
                headroom_kwh = max(0.0, max_soc_kwh - current_soc_kwh)
                passive_charge_kwh = min(
                    pv_dc_forecast[t] * pv_dc_efficiency * step_h,
                    headroom_kwh,
                )
                current_soc_kwh += passive_charge_kwh
            actual_power_kw = 0.0

        rebuilt_power[t] = actual_power_kw
        if actual_power_kw > POWER_IDLE_THRESHOLD_KW:
            rebuilt_mode.append(ACTION_CHARGING)
        elif actual_power_kw < -POWER_IDLE_THRESHOLD_KW:
            rebuilt_mode.append(ACTION_DISCHARGING)
        else:
            rebuilt_mode.append(ACTION_IDLE)
        soc_schedule.append(current_soc_kwh)

    return rebuilt_power, rebuilt_mode, soc_schedule


def _calculate_baseline_cost(
    price_forecast: list[float],
    feed_in_forecast: list[float],
    pv_forecast: list[float],
    consumption_forecast: list[float],
    step_durations_hours: list[float],
    pv_dc_forecast: list[float],
    max_grid_power_kw: float = 0.0,
) -> float:
    """Calculate horizon cost without battery action.

    Baseline scenario: no battery exists. DC-coupled PV panels would still
    produce power, but without a battery to absorb it, all DC PV goes through
    the inverter to AC (at DC_TO_AC_INVERTER_EFFICIENCY).

    The grid capacity cap (max_grid_power_kw, 0 = unlimited) is applied the
    same way as in calculate_step_cost: without it, the baseline could "sell"
    PV beyond the physical grid connection, inflating baseline revenue and
    distorting the reported savings.
    """
    baseline_cost = 0.0
    n_steps = min(len(price_forecast), len(pv_forecast), len(consumption_forecast))
    for t in range(n_steps):
        time_step_hours = step_durations_hours[t]
        grid_price = price_forecast[t]
        feed_in_price = feed_in_forecast[t] if t < len(feed_in_forecast) else grid_price
        pv_w = pv_forecast[t] * 1000 if t < len(pv_forecast) else 0
        pv_dc_w = pv_dc_forecast[t] * 1000 if t < len(pv_dc_forecast) else 0
        consumption_w = (
            consumption_forecast[t] * 1000 if t < len(consumption_forecast) else 0
        )
        dc_pv_to_ac_w = pv_dc_w * DC_TO_AC_INVERTER_EFFICIENCY if pv_dc_w > 0 else 0
        total_pv_w = pv_w + dc_pv_to_ac_w
        net_grid_w = consumption_w - total_pv_w
        if max_grid_power_kw > 0:
            cap_w = max_grid_power_kw * 1000
            net_grid_w = max(-cap_w, min(cap_w, net_grid_w))
        energy_kwh = abs(net_grid_w) * time_step_hours / 1000
        if net_grid_w > 0:
            baseline_cost += energy_kwh * grid_price
        else:
            baseline_cost -= energy_kwh * feed_in_price
    return baseline_cost


def _calculate_schedule_total_cost(
    battery_config: BatteryConfig,
    power_schedule_kw: list[float],
    soc_schedule_kwh: list[float],
    price_forecast: list[float],
    feed_in_forecast: list[float],
    pv_forecast: list[float],
    consumption_forecast: list[float],
    step_durations_hours: list[float],
    degradation_cost_per_kwh: float,
    terminal_price: float,
    pv_dc_forecast: list[float] | None = None,
) -> float:
    """Calculate total cost for the final post-processed schedule.

    Note: This rebuilds SoC step-by-step from the post-processed power schedule,
    which may differ slightly from the DP's internal SoC due to floating-point
    arithmetic. The difference is typically < 0.001 EUR and is inherent to the
    discretisation — it cannot be eliminated without re-running the DP itself.
    """
    if pv_dc_forecast is None:
        pv_dc_forecast = [0.0] * len(power_schedule_kw)

    total_cost = 0.0
    for t, power_kw in enumerate(power_schedule_kw):
        total_cost += calculate_step_cost(
            time_step_hours=step_durations_hours[t],
            soc_wh=soc_schedule_kwh[t] * 1000,
            action_w=power_kw * 1000,
            grid_price=price_forecast[t],
            feed_in_price=feed_in_forecast[t]
            if t < len(feed_in_forecast)
            else price_forecast[t],
            pv_production_w=(pv_forecast[t] if t < len(pv_forecast) else 0.0) * 1000,
            consumption_w=(
                consumption_forecast[t] if t < len(consumption_forecast) else 0.0
            )
            * 1000,
            rte=battery_config.round_trip_efficiency,
            degradation_cost_per_kwh=degradation_cost_per_kwh,
            battery_config=battery_config,
            pv_dc_production_w=(pv_dc_forecast[t] if t < len(pv_dc_forecast) else 0.0)
            * 1000,
        )

    final_soc_kwh = soc_schedule_kwh[-1] if soc_schedule_kwh else 0.0
    final_stored_kwh = max(0.0, final_soc_kwh - battery_config.min_soc_kwh)
    total_cost -= final_stored_kwh * terminal_price
    return total_cost


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
       - action_w is the AC power setpoint (what the inverter is told to do)
       - charge_efficiency = sqrt(RTE) ~ 0.95 for RTE=0.90
       - discharge_efficiency = sqrt(RTE) ~ 0.95
       - Charging: grid draws the AC setpoint directly; losses are internal to
         the inverter, captured in the SoC transition (battery stores action_w * sqrt_rte)
       - Discharging: AC output = abs(action_w) (the setpoint); battery-side draw
         is action_w / sqrt_rte (also captured in SoC transition)
       - Throughput for degradation: charging = action_w * sqrt_rte * dt / 1000 (Wh
         actually stored); discharging = abs(action_w) / sqrt_rte * dt / 1000 (Wh drawn)

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

    Returns:
        Total cost in EUR for this time step
    """
    sqrt_rte = math.sqrt(rte)
    charge_eff = sqrt_rte
    discharge_eff = sqrt_rte
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

        # AC charging: grid draws the AC setpoint directly; losses are internal
        # to the inverter, captured in the SoC transition
        grid_to_battery_w = ac_charge_w  # grid draws AC setpoint power

        throughput_kwh = (
            action_w * time_step_hours * charge_eff / 1000
        )  # actual stored Wh

    elif action_w < 0:  # DISCHARGING
        # All DC PV excess goes to AC side when discharging
        dc_pv_excess_w = pv_dc_production_w

        # AC output = discharge setpoint (no multiplier needed)
        usable_power_w = abs(action_w)  # AC output = discharge setpoint
        grid_to_battery_w = -usable_power_w  # Negative = to home

        throughput_kwh = (
            abs(action_w) * time_step_hours / discharge_eff / 1000
        )  # actual battery-drawn Wh

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
    charge_eff_override: float | None = None,  # Override charge-side efficiency only
    discharge_eff_override: float
    | None = None,  # Override discharge-side efficiency only
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
        charge_eff_override: Override for the charge-side SoC transition only.
            When provided, charging state transitions use this value instead of
            sqrt(RTE) so the DP plans less charge within the step when charging
            is slower than modelled. The economic cost model still uses nominal
            sqrt(RTE).
        discharge_eff_override: Override for the discharge-side SoC transition only.
            When provided, discharging state transitions use this value instead of
            sqrt(RTE) so the DP plans less SoC depletion within the step when
            discharging is slower than modelled. May be > 1 (calibration artefact);
            this only affects SoC state transitions, not the economic cost model.

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
    # Charging uses the (possibly corrected) efficiency; discharging uses its own
    # correction independently. Separating them prevents a speed correction on one
    # side from inflating the break-even price on the other.
    # Guard against zero or negative efficiency overrides to prevent
    # ZeroDivisionError in SoC transition calculations and _rebuild_schedule.
    if charge_eff_override is not None and charge_eff_override <= 0.0:
        charge_eff_override = None
    if discharge_eff_override is not None and discharge_eff_override <= 0.0:
        discharge_eff_override = None
    charge_eff = charge_eff_override if charge_eff_override is not None else sqrt_rte
    discharge_eff = (
        discharge_eff_override if discharge_eff_override is not None else sqrt_rte
    )

    # Discretize SoC space.
    min_step_hours = min(step_durations_hours[:n_steps])

    # SoC resolution: use the constant directly.
    # Previously inflated to max(SOC_RESOLUTION_WH, POWER_STEP_W * min_step_hours
    # * sqrt_rte) to ensure the minimum power action always moved at least one
    # state.  With hourly prices this inflated to ~87 Wh (only 22 states for a
    # 2 kWh battery), causing the DP to coarsely map post-discharge SoC and
    # systematically undervalue concentrating discharge at the peak-price hour.
    # The per-action sub-resolution guard (new_soc_idx == s_idx → skip) already
    # handles steps where a small action cannot cross a state boundary, so the
    # inflation is unnecessary.
    soc_resolution_wh = float(SOC_RESOLUTION_WH)

    # Power step: POWER_STEP_W as minimum practical granularity.  The aligned
    # step (SOC_RES / full_step_hours) ensures the smallest action crosses at
    # least one SoC state; the max() keeps it at 100 W so near-marginal prices
    # don't produce unpractical trickle-charge/discharge actions.
    # Boundary actions (drain-to-min / fill-to-max) are evaluated separately
    # below to capture the last ~50 Wh that the 100 W grid cannot reach.
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
    # Use the 6-hour tail average of the feed-in forecast rather than the
    # shadow price from the previous run.  The shadow price λ ≈ sqrt(RTE) ×
    # P_best, so using it as terminal_price makes discharge at P_best break-
    # even (opportunity cost = λ / sqrt(RTE) = P_best).  In a rolling-horizon
    # re-optimisation the "best" hours are often the current hours, so this
    # circular dependency suppresses discharge exactly at the peak.  The tail
    # average is naturally below peak prices and avoids this trap.
    # terminal_shadow_price is still passed to the caller and used by hybrid
    # mode as the charge/discharge switching threshold — it is just no longer
    # used to initialise V[T].
    # The lookback window is time-based (6 h) so behaviour is identical for
    # 15-min, 30-min and 60-min price intervals.
    #
    # Clipped tail average: each price in the tail is capped at the median of
    # the full forecast before averaging.  This prevents an evening price spike
    # at the end of the horizon from inflating the terminal value and
    # suppressing discharge at those same hours within the horizon — the same
    # self-suppression the shadow price had, but for end-of-horizon peaks.
    # When the tail contains no outliers the clip has no effect.
    if feed_in_forecast:
        lookback = max(1, min(round(6.0 / full_step_hours), len(feed_in_forecast)))
        sorted_prices = sorted(feed_in_forecast)
        median_price = sorted_prices[len(sorted_prices) // 2]
        clipped_tail = [min(p, median_price) for p in feed_in_forecast[-lookback:]]
        avg_tail = sum(clipped_tail) / len(clipped_tail)
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

    # Pre-compute SoC-dependent power limits for every SoC state.
    # For batteries without derating these equal max_charge_w / max_discharge_w
    # and the guards below never fire, so there is no performance regression.
    soc_max_charge_w = [
        battery_config.max_charge_at_soc(s_wh / 1000) * 1000 for s_wh in soc_states
    ]
    soc_max_discharge_w = [
        battery_config.max_discharge_at_soc(s_wh / 1000) * 1000 for s_wh in soc_states
    ]

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
            max_chg_w = soc_max_charge_w[s_idx]
            max_dis_w = soc_max_discharge_w[s_idx]

            for action_w in actions:
                # SoC transition: action_w is battery-side power (explicit AC command).
                # For DC-coupled systems in idle mode (action_w == 0), the MPPT
                # charger passively charges the battery from DC PV up to available
                # headroom — the AC setpoint of 0 only stops AC-side grid charging.
                # Efficiency losses are on the grid/AC side and handled in
                # calculate_step_cost.
                if action_w > 0:
                    if action_w > max_chg_w:
                        continue
                    energy_change_wh = action_w * time_step_hours * charge_eff
                    new_soc_wh = soc_wh + energy_change_wh
                    if new_soc_wh > max_soc_wh:
                        continue
                elif action_w < 0:
                    if -action_w > max_dis_w:
                        continue
                    # Discharge: action_w is AC setpoint → battery must supply
                    # abs(action_w) / discharge_eff from its DC side.
                    energy_change_wh = abs(action_w) * time_step_hours / discharge_eff
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

            # Boundary actions: exact power to reach min/max SoC in this step.
            # new_soc_idx is known directly (0 or n_soc_states-1), avoiding the
            # floating-point round-trip through the energy formula.
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

    # Find current SoC index in the DP state space (needed for forward pass).
    # Shadow price is computed after post-processing filters below.
    current_soc_wh = int(current_soc_kwh * 1000)
    current_soc_idx = _find_nearest_soc_idx(current_soc_wh, soc_states)

    # Forward pass: extract optimal schedule

    power_schedule_kw = []
    mode_schedule = []
    soc_schedule_kwh = [current_soc_kwh]

    # Forward pass: V-table re-evaluation from actual continuous SoC.
    # Instead of snapping to the nearest discrete state and following the policy
    # table, we keep the real SoC and at each step enumerate the same action set
    # as the backward pass (including boundary actions), evaluating
    # step_cost + V[t+1][new_soc_idx] directly. This eliminates the SoC
    # discretisation error that accumulates when the policy for a neighbouring
    # discrete state differs from the optimal action at the true SoC.
    current_soc = current_soc_kwh * 1000.0  # continuous, not snapped

    for t in range(n_steps):
        time_step_hours = step_durations_hours[t]
        grid_price = price_forecast[t]
        feed_in_price = feed_in_forecast[t] if t < len(feed_in_forecast) else grid_price
        pv_w = pv_forecast[t] * 1000 if t < len(pv_forecast) else 0
        pv_dc_w = pv_dc_forecast[t] * 1000 if t < len(pv_dc_forecast) else 0.0
        consumption_w = (
            consumption_forecast[t] * 1000 if t < len(consumption_forecast) else 0
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
            mode_schedule.append(ACTION_CHARGING)
        elif best_action < 0:
            mode_schedule.append(ACTION_DISCHARGING)
        else:
            mode_schedule.append(ACTION_IDLE)
        current_soc = best_new_soc
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
        initial_soc_kwh=soc_schedule_kwh[0],
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
        pv_dc_forecast=pv_dc_forecast[:n_steps] if pv_dc_forecast else None,
        pv_dc_coupled=battery_config.pv_dc_coupled,
        pv_dc_efficiency=battery_config.pv_dc_efficiency,
        discharge_eff_override=discharge_eff_override,
        charge_eff_override=charge_eff_override,
    )

    # Post-process: suppress micro-cycles (P5.1)
    power_schedule_kw, mode_schedule, soc_schedule_kwh = _filter_micro_cycles(
        power_schedule_kw=power_schedule_kw,
        mode_schedule=mode_schedule,
        initial_soc_kwh=soc_schedule_kwh[0],
        step_durations_hours=step_durations_hours[:n_steps],
        rte=battery_config.round_trip_efficiency,
        min_soc_kwh=battery_config.min_soc_kwh,
        max_soc_kwh=battery_config.max_soc_kwh,
        pv_dc_forecast=pv_dc_forecast[:n_steps] if pv_dc_forecast else None,
        pv_dc_coupled=battery_config.pv_dc_coupled,
        pv_dc_efficiency=battery_config.pv_dc_efficiency,
        min_cycle_kwh=MIN_CYCLE_KWH,
        discharge_eff_override=discharge_eff_override,
        charge_eff_override=charge_eff_override,
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
    raw_shadow_price_eur_kwh = 0.0
    if n_soc_states >= 3 and 0 < current_soc_idx < n_soc_states - 1:
        raw_shadow_price_eur_kwh = (
            V[0][current_soc_idx - 1] - V[0][current_soc_idx + 1]
        ) / (2 * step_kwh)
    elif n_soc_states >= 2:
        if current_soc_idx == 0:
            raw_shadow_price_eur_kwh = (V[0][0] - V[0][1]) / step_kwh
        else:
            raw_shadow_price_eur_kwh = (V[0][-2] - V[0][-1]) / step_kwh

    # Raw DP cost before any post-processing filters.
    raw_total_cost = V[0][current_soc_idx]

    baseline_cost = _calculate_baseline_cost(
        price_forecast=price_forecast[:n_steps],
        feed_in_forecast=(
            feed_in_forecast[:n_steps] if feed_in_forecast else price_forecast[:n_steps]
        ),
        pv_forecast=pv_forecast[:n_steps],
        consumption_forecast=consumption_forecast[:n_steps],
        step_durations_hours=step_durations_hours[:n_steps],
        pv_dc_forecast=pv_dc_forecast[:n_steps],
        max_grid_power_kw=battery_config.max_grid_power_kw,
    )

    # Savings = value added by battery ACTIONS only.
    # total_cost already contains the terminal value of stored energy at horizon end.
    # baseline_cost does not include any terminal value.
    # Subtracting the terminal value of the *initial* SoC makes savings = 0 when the
    # battery is idle, regardless of how much energy is already stored.
    initial_stored_kwh = max(0.0, current_soc_kwh - battery_config.min_soc_kwh)
    initial_terminal_value = initial_stored_kwh * terminal_price
    raw_savings = baseline_cost - initial_terminal_value - raw_total_cost
    total_cost = _calculate_schedule_total_cost(
        battery_config=battery_config,
        power_schedule_kw=power_schedule_kw,
        soc_schedule_kwh=soc_schedule_kwh,
        price_forecast=price_forecast[:n_steps],
        feed_in_forecast=(
            feed_in_forecast[:n_steps] if feed_in_forecast else price_forecast[:n_steps]
        ),
        pv_forecast=pv_forecast[:n_steps],
        consumption_forecast=consumption_forecast[:n_steps],
        step_durations_hours=step_durations_hours[:n_steps],
        degradation_cost_per_kwh=degradation_cost_per_kwh,
        terminal_price=terminal_price,
        pv_dc_forecast=pv_dc_forecast[:n_steps],
    )
    savings = baseline_cost - initial_terminal_value - total_cost

    setpoint_power_kw = power_schedule_kw[0] if power_schedule_kw else 0.0
    setpoint_mode = mode_schedule[0] if mode_schedule else ACTION_IDLE

    return OptimizationResult(
        power_schedule_kw=power_schedule_kw,
        mode_schedule=mode_schedule,
        soc_schedule_kwh=soc_schedule_kwh,
        total_cost=total_cost,
        baseline_cost=baseline_cost,
        savings=savings,
        optimal_power_kw=setpoint_power_kw,
        optimal_mode=setpoint_mode,
        shadow_price_eur_kwh=raw_shadow_price_eur_kwh,
        price_forecast=list(price_forecast[:n_steps]),
        pv_forecast=list(pv_forecast[:n_steps]),
        consumption_forecast=list(consumption_forecast[:n_steps]),
        raw_total_cost=raw_total_cost,
        raw_savings=raw_savings,
    )


def _filter_oscillations(
    power_schedule_kw: list[float],
    mode_schedule: list[str],
    initial_soc_kwh: float,
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
    pv_dc_forecast: list[float] | None = None,
    pv_dc_coupled: bool = False,
    pv_dc_efficiency: float = 0.97,
    discharge_eff_override: float | None = None,
    charge_eff_override: float | None = None,
) -> tuple[list[float], list[str], list[float]]:
    """Filter out unprofitable oscillations from the schedule.

    Removes rapid charge/discharge switches that don't have sufficient
    price spread to justify the round-trip efficiency losses and degradation.

    Takes into account PV surplus opportunity cost (feed-in price) when
    evaluating charging profitability.

    Args:
        power_schedule_kw: Power schedule in kW
        mode_schedule: Mode schedule
        initial_soc_kwh: Initial SoC in kWh (only element [0] of soc_schedule is needed)
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
        return power_schedule_kw, mode_schedule, [initial_soc_kwh]

    sqrt_rte = math.sqrt(rte)
    filtered_power = list(power_schedule_kw)
    filtered_mode = list(mode_schedule)
    # Minimum profitable price spread needed for arbitrage
    # P_discharge * sqrt(rte) > P_charge / sqrt(rte) + 2 * degradation + min_spread
    # => P_discharge > P_charge / rte + (2 * degradation + min_spread) / sqrt(rte)
    min_arbitrage_spread = (2 * degradation_cost_per_kwh + min_price_spread) / sqrt_rte

    def get_charge_cost(timestep: int, charge_power_kw: float) -> float:
        """Get the marginal cost of charging for the actual commanded power.

        For DC-coupled PV: passive charging happens even at idle (free). Only the
        portion of active charging above the passive DC PV contribution costs money.
        """
        if (
            charge_power_kw <= 0
            or not pv_forecast
            or not consumption_forecast
            or not feed_in_forecast
        ):
            return price_forecast[timestep]

        # DC PV passive charge at idle is free — only charge above this costs money
        effective_charge_kw = charge_power_kw
        if pv_dc_coupled and pv_dc_forecast:
            step_h = (
                step_durations_hours[timestep]
                if timestep < len(step_durations_hours)
                else step_durations_hours[-1]
            )
            passive_charge_kw = (
                pv_dc_forecast[timestep] * pv_dc_efficiency
                if timestep < len(pv_dc_forecast)
                else 0.0
            )
            # Cap passive charge by available headroom (approximate using max_soc)
            max_passive_kwh = (max_soc_kwh - min_soc_kwh) if step_h > 0 else 0.0
            passive_charge_kw = (
                min(passive_charge_kw, max_passive_kwh / step_h) if step_h > 0 else 0.0
            )
            effective_charge_kw = max(0.0, charge_power_kw - passive_charge_kw)
            if effective_charge_kw <= 0:
                return 0.0  # All charging is passive DC PV, no cost

        pv_surplus_kw = max(0.0, pv_forecast[timestep] - consumption_forecast[timestep])
        from_pv_kw = min(effective_charge_kw, pv_surplus_kw)
        from_grid_kw = max(0.0, effective_charge_kw - from_pv_kw)
        total_kw = from_pv_kw + from_grid_kw
        if total_kw <= 0:
            return price_forecast[timestep]
        return (
            from_pv_kw * feed_in_forecast[timestep]
            + from_grid_kw * price_forecast[timestep]
        ) / total_kw

    def get_discharge_value(timestep: int, discharge_power_kw: float) -> float:
        """Get the marginal value of discharging for the actual commanded power.

        For DC-coupled PV: discharging displaces energy that would otherwise come
        from passive DC PV charging (opportunity cost). The effective value is
        reduced by the portion that merely offsets free DC PV.
        """
        if discharge_power_kw <= 0:
            return price_forecast[timestep]
        if not pv_forecast or not consumption_forecast or not feed_in_forecast:
            return price_forecast[timestep]

        residual_load_kw = max(
            0.0, consumption_forecast[timestep] - pv_forecast[timestep]
        )
        to_self_kw = min(discharge_power_kw, residual_load_kw)
        to_export_kw = max(0.0, discharge_power_kw - to_self_kw)
        total_kw = to_self_kw + to_export_kw
        if total_kw <= 0:
            return price_forecast[timestep]
        return (
            to_self_kw * price_forecast[timestep]
            + to_export_kw * feed_in_forecast[timestep]
        ) / total_kw

    # Lookahead window: use the full interval step (index 1), not the partial
    # first step (index 0).  The first step is artificially shortened to align
    # with the current price-period boundary (e.g. 1 minute when running just
    # before a 15-min tick).  Using it would inflate lookahead_steps by up to
    # 15× for quarter-hour data (round(2.0 / 0.017) = 120 instead of 8),
    # causing the filter to scan the entire 36-h horizon and pair charge steps
    # with distant, unrelated discharge steps — incorrectly suppressing whole
    # charge blocks.
    ref_step_h = (
        step_durations_hours[1]
        if len(step_durations_hours) > 1
        else (step_durations_hours[0] if step_durations_hours else 0.25)
    )
    lookahead_steps = max(1, round(oscillation_window_hours / ref_step_h))

    # Iterative scan: repeat until convergence so that suppressing one step
    # also triggers re-evaluation of any steps that depended on it (orphaned
    # discharge after paired charge is suppressed, and vice-versa).
    changed = True
    while changed:
        changed = False
        i = 0
        while i < len(filtered_mode) - 1:
            if filtered_mode[i] == ACTION_CHARGING:
                # Look ahead for a discharge within the oscillation window.
                # Evaluate ALL discharges in the window: only suppress the
                # charge if every discharge found is unprofitable.  Stopping
                # at the *nearest* discharge was wrong for quarter-hour data:
                # the DP can produce a complex schedule where a small
                # intermediate discharge (low spread) precedes the main
                # discharge (high spread).  Breaking on the first discharge
                # caused the charge to be removed even though a profitable
                # pairing existed slightly further in the window.
                charge_cost = get_charge_cost(i, max(filtered_power[i], 0.0))
                has_discharge_in_window = False
                has_profitable_discharge = False
                for j in range(i + 1, min(i + lookahead_steps + 1, len(filtered_mode))):
                    if filtered_mode[j] == ACTION_DISCHARGING:
                        has_discharge_in_window = True
                        discharge_price = get_discharge_value(j, abs(filtered_power[j]))
                        effective_spread = discharge_price - charge_cost / rte
                        if effective_spread >= min_arbitrage_spread:
                            has_profitable_discharge = True
                            break
                if has_discharge_in_window and not has_profitable_discharge:
                    filtered_power[i] = 0.0
                    filtered_mode[i] = ACTION_IDLE
                    changed = True
            elif filtered_mode[i] == ACTION_DISCHARGING:
                # Look ahead for a charge within the oscillation window.
                # Same logic: suppress only if every charge in the window
                # would make this discharge unprofitable.
                discharge_price = get_discharge_value(i, abs(filtered_power[i]))
                has_charge_in_window = False
                has_profitable_charge = False
                for j in range(i + 1, min(i + lookahead_steps + 1, len(filtered_mode))):
                    if filtered_mode[j] == ACTION_CHARGING:
                        has_charge_in_window = True
                        charge_cost = get_charge_cost(j, max(filtered_power[j], 0.0))
                        effective_spread = discharge_price - charge_cost / rte
                        if effective_spread >= min_arbitrage_spread:
                            has_profitable_charge = True
                            break
                if has_charge_in_window and not has_profitable_charge:
                    filtered_power[i] = 0.0
                    filtered_mode[i] = ACTION_IDLE
                    changed = True
            i += 1

    return _rebuild_schedule(
        power_schedule_kw=filtered_power,
        step_durations_hours=step_durations_hours,
        initial_soc_kwh=initial_soc_kwh,
        min_soc_kwh=min_soc_kwh,
        max_soc_kwh=max_soc_kwh,
        rte=rte,
        pv_dc_forecast=pv_dc_forecast,
        pv_dc_coupled=pv_dc_coupled,
        pv_dc_efficiency=pv_dc_efficiency,
        discharge_eff=discharge_eff_override,
        charge_eff=charge_eff_override,
    )


def _filter_micro_cycles(
    power_schedule_kw: list[float],
    mode_schedule: list[str],
    initial_soc_kwh: float,
    step_durations_hours: list[float],
    rte: float,
    min_soc_kwh: float,
    max_soc_kwh: float,
    pv_dc_forecast: list[float] | None = None,
    pv_dc_coupled: bool = False,
    pv_dc_efficiency: float = 0.97,
    min_cycle_kwh: float = 0.2,
    discharge_eff_override: float | None = None,
    charge_eff_override: float | None = None,
) -> tuple[list[float], list[str], list[float]]:
    """Filter out micro-cycles whose total energy is below min_cycle_kwh.

    Charge or discharge segments that move less energy than min_cycle_kwh have
    disproportionately high degradation cost per kWh of useful storage.  Replacing
    them with idle preserves battery lifespan without meaningful cost impact.

    Args:
        power_schedule_kw: Power schedule in kW
        mode_schedule: Mode schedule
        initial_soc_kwh: Initial SoC in kWh
        step_durations_hours: Per-step duration in hours
        min_cycle_kwh: Minimum energy per charge/discharge block in kWh

    Returns:
        Filtered (power_schedule, mode_schedule, soc_schedule)
    """
    if not power_schedule_kw:
        return power_schedule_kw, mode_schedule, [initial_soc_kwh]

    filtered_power = list(power_schedule_kw)
    filtered_mode = list(mode_schedule)
    any_filtered = False

    i = 0
    while i < len(filtered_mode):
        current_dir = filtered_mode[i]
        if current_dir not in (ACTION_CHARGING, ACTION_DISCHARGING):
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
                filtered_mode[k] = ACTION_IDLE
            any_filtered = True

        i = j

    if not any_filtered:
        # Nothing changed; skip rebuild since oscillation filter already produced
        # consistent power/mode/soc. Rebuild only needed to get soc_schedule.
        return (
            power_schedule_kw,
            mode_schedule,
            _rebuild_schedule(
                power_schedule_kw=power_schedule_kw,
                step_durations_hours=step_durations_hours,
                initial_soc_kwh=initial_soc_kwh,
                min_soc_kwh=min_soc_kwh,
                max_soc_kwh=max_soc_kwh,
                rte=rte,
                pv_dc_forecast=pv_dc_forecast,
                pv_dc_coupled=pv_dc_coupled,
                pv_dc_efficiency=pv_dc_efficiency,
                discharge_eff=discharge_eff_override,
                charge_eff=charge_eff_override,
            )[2],
        )

    return _rebuild_schedule(
        power_schedule_kw=filtered_power,
        step_durations_hours=step_durations_hours,
        initial_soc_kwh=initial_soc_kwh,
        min_soc_kwh=min_soc_kwh,
        max_soc_kwh=max_soc_kwh,
        rte=rte,
        pv_dc_forecast=pv_dc_forecast,
        pv_dc_coupled=pv_dc_coupled,
        pv_dc_efficiency=pv_dc_efficiency,
        discharge_eff=discharge_eff_override,
        charge_eff=charge_eff_override,
    )


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
        optimal_mode=ACTION_IDLE,
        shadow_price_eur_kwh=0.0,
        price_forecast=[],
        pv_forecast=[],
        consumption_forecast=[],
    )

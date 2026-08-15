"""Dynamic Programming optimizer for battery scheduling."""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from .battery_model import BatteryConfig
from .efficiency_curve import (
    EfficiencyCurve,
    interpolate_efficiency,
    representative_efficiency,
)
from .const import (
    ACTION_CHARGING,
    ACTION_DISCHARGING,
    ACTION_IDLE,
    DC_TO_AC_INVERTER_EFFICIENCY,
    MAX_SOC_STATES,
    MIN_CYCLE_KWH,
    POWER_IDLE_THRESHOLD_KW,
    POWER_STEP_W,
    SOC_RESOLUTION_WH,
)

_LOGGER = logging.getLogger(__name__)


def grid_cap_charge_limit_w(
    max_grid_power_kw: float, consumption_w: float, ac_pv_w: float
) -> float:
    """Largest AC charge setpoint that keeps grid import within the connection cap.

    The cap is a property of the physical grid connection, so it has to bound
    the *action*, not the price of the resulting flow.  Clipping ``net_grid_w``
    on the import side instead (as this model used to do) made every watt above
    the cap free: the SoC still rose while the cost stopped growing, so the DP
    charged at full power whenever the cap was binding and planned imports of
    nearly twice the configured limit.  Only the export side is still clipped,
    where zero-revenue clipping is the correct model of curtailment.

    ``ac_pv_w`` is AC-side PV only.  DC-coupled PV is deliberately left out: the
    share of it that reaches the AC bus depends on how much the battery absorbs,
    which depends on the SoC, and assuming none of it does is the bound that
    cannot be violated.  Near full SoC — the one case where the assumption is
    pessimistic — there is barely any headroom left to charge into anyway.

    Returns ``inf`` when no cap is configured (0 = unlimited).
    """
    if max_grid_power_kw <= 0:
        return float("inf")
    return max(0.0, max_grid_power_kw * 1000.0 - consumption_w + ac_pv_w)


def passive_dc_charge_wh(
    pv_dc_production_w: float,
    dc_efficiency: float,
    time_step_hours: float,
    headroom_wh: float,
    budget_wh: float,
) -> float:
    """Energy the DC MPPT absorbs into the battery over one step, in Wh.

    Bounded by three things: what the panels produce, the SoC headroom that is
    left, and the battery's remaining charge-power budget for the step.  The
    last bound is what stops a DC array larger than the inverter from charging
    the battery faster than it can physically be charged — passive MPPT power
    used to be limited by headroom alone, so a 6 kW array on a 5 kW inverter
    had the DP planning 5.8 kW into the pack, and an AC charge setpoint stacked
    on top of that unchecked.
    """
    return max(
        0.0,
        min(
            pv_dc_production_w * dc_efficiency * time_step_hours,
            headroom_wh,
            budget_wh,
        ),
    )


def charge_budget_wh(
    max_charge_power_kw: float, time_step_hours: float, ac_stored_wh: float
) -> float:
    """Battery-side charge energy still available in this step, in Wh.

    The rating is read as a battery-side limit on the *combined* charge path, so
    the energy an AC setpoint already stores is deducted before the passive DC
    path may use the rest.  Deliberately the nominal rating rather than
    ``max_charge_at_soc``: the SoC-dependent derating only bites near full SoC,
    where the headroom bound in :func:`passive_dc_charge_wh` binds first anyway,
    and keeping this term independent of the SoC is what lets the DP cache one
    step cost per action instead of one per action per SoC state.
    """
    return max(0.0, max_charge_power_kw * 1000.0 * time_step_hours - ac_stored_wh)


def compute_soc_resolution_wh(min_soc_wh: float, max_soc_wh: float) -> float:
    """Return the DP SoC grid resolution in Wh for a usable range.

    SOC_RESOLUTION_WH, coarsened only when the range is large enough that the
    state count would exceed MAX_SOC_STATES. DP cost scales with the state
    count, so without this a large battery pays a proportionally longer solve
    purely for being large. Kept as a module-level function so the state-space
    budget can be asserted directly in tests.
    """
    return max(float(SOC_RESOLUTION_WH), (max_soc_wh - min_soc_wh) / MAX_SOC_STATES)


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
    # Use as a threshold: charging 1 kWh AC stores sqrt(RTE) kWh worth λ each, so
    # charge when buy_price < shadow_price × sqrt(RTE). Discharging 1 stored kWh
    # yields sqrt(RTE) kWh AC, so discharge/export when
    # feed_in_price > shadow_price / sqrt(RTE).
    shadow_price_eur_kwh: float

    # Metadata
    price_forecast: list[float]
    pv_forecast: list[float]
    consumption_forecast: list[float]
    raw_total_cost: float | None = None
    raw_savings: float | None = None


def _rebuild_charge_budget_wh(
    max_charge_power_kw: float, step_hours: float, ac_stored_wh: float
) -> float:
    """:func:`charge_budget_wh`, with 0 kW meaning "no rating known, no bound"."""
    if max_charge_power_kw <= 0:
        return float("inf")
    return charge_budget_wh(max_charge_power_kw, step_hours, ac_stored_wh)


def _rebuild_schedule(
    power_schedule_kw: list[float],
    step_durations_hours: list[float],
    initial_soc_kwh: float,
    min_soc_kwh: float,
    max_soc_kwh: float,
    charge_curve: EfficiencyCurve,
    discharge_curve: EfficiencyCurve,
    pv_dc_forecast: list[float] | None = None,
    pv_dc_coupled: bool = False,
    pv_dc_efficiency: float = 0.97,
    max_charge_power_kw: float = 0.0,
) -> tuple[list[float], list[str], list[float]]:
    """Rebuild schedule after post-processing so SoC stays physically consistent.

    ``max_charge_power_kw`` bounds the passive DC MPPT path the same way the DP
    does; 0 disables that bound so callers without a rating keep the previous
    headroom-only behaviour.
    """
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
            _charge_eff = interpolate_efficiency(charge_curve, abs(commanded_power_kw))
            current_soc_kwh = min(
                current_soc_kwh + commanded_power_kw * step_h * _charge_eff,
                max_soc_kwh,
            )
            delta_soc = current_soc_kwh - prev_soc_kwh
            actual_power_kw = delta_soc / (step_h * _charge_eff) if step_h > 0 else 0.0
            if pv_dc_coupled and t < len(pv_dc_forecast) and pv_dc_forecast[t] > 0:
                # Passive DC MPPT charging continues on top of the AC charge,
                # sharing the step's charge-power budget with it.
                headroom_kwh = max(0.0, max_soc_kwh - current_soc_kwh)
                current_soc_kwh += (
                    passive_dc_charge_wh(
                        pv_dc_forecast[t] * 1000.0,
                        pv_dc_efficiency,
                        step_h,
                        headroom_kwh * 1000.0,
                        _rebuild_charge_budget_wh(
                            max_charge_power_kw, step_h, delta_soc * 1000.0
                        ),
                    )
                    / 1000.0
                )
        elif commanded_power_kw < 0:
            _discharge_eff = interpolate_efficiency(
                discharge_curve, abs(commanded_power_kw)
            )
            current_soc_kwh = max(
                current_soc_kwh - abs(commanded_power_kw) * step_h / _discharge_eff,
                min_soc_kwh,
            )
            delta_soc = current_soc_kwh - prev_soc_kwh
            actual_power_kw = delta_soc * _discharge_eff / step_h if step_h > 0 else 0.0
        else:
            if pv_dc_coupled and t < len(pv_dc_forecast) and pv_dc_forecast[t] > 0:
                headroom_kwh = max(0.0, max_soc_kwh - current_soc_kwh)
                current_soc_kwh += (
                    passive_dc_charge_wh(
                        pv_dc_forecast[t] * 1000.0,
                        pv_dc_efficiency,
                        step_h,
                        headroom_kwh * 1000.0,
                        _rebuild_charge_budget_wh(max_charge_power_kw, step_h, 0.0),
                    )
                    / 1000.0
                )
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
    same way as in calculate_step_cost — export only. Without the export clip
    the baseline could "sell" PV beyond the physical grid connection, inflating
    baseline revenue and distorting the reported savings. Import is left
    unclipped in both places: the baseline has no battery action to constrain,
    so clipping it there would simply hand the no-battery house free energy and
    understate the savings the battery actually delivers.
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
            net_grid_w = max(-max_grid_power_kw * 1000, net_grid_w)
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
            charge_curve=battery_config.charge_efficiency_curve_parsed,
            discharge_curve=battery_config.discharge_efficiency_curve_parsed,
            degradation_cost_per_kwh=degradation_cost_per_kwh,
            battery_config=battery_config,
            pv_dc_production_w=(pv_dc_forecast[t] if t < len(pv_dc_forecast) else 0.0)
            * 1000,
        )

    final_soc_kwh = soc_schedule_kwh[-1] if soc_schedule_kwh else 0.0
    final_stored_kwh = max(0.0, final_soc_kwh - battery_config.min_soc_kwh)
    total_cost -= final_stored_kwh * terminal_price
    return total_cost


# Boundary-power fixed point: iterate until the efficiency stops moving.  A flat
# curve converges on the first pass; a steep part-load curve needs a handful.
_BOUNDARY_SOLVER_MAX_ITER = 12
_BOUNDARY_SOLVER_TOL = 1e-9


def solve_boundary_drain_w(
    delta_wh: float,
    step_hours: float,
    discharge_curve: EfficiencyCurve,
    seed_eff: float,
) -> float:
    """AC discharge power that draws exactly ``delta_wh`` from the battery.

    The discharge SoC transition is ``delta = power * hours / eff(power)``, so
    the power to hit an exact SoC boundary appears on both sides of the
    equation.  Solve it by fixed-point iteration seeded with the representative
    scalar; two passes converge because the curves are smooth and the residual
    capacities involved are small.  Using a single zero-power scalar instead
    would be wrong by tens of percentage points on a steep curve, because
    boundary powers land in exactly the steep part-load region.
    """
    eff = seed_eff
    for _ in range(_BOUNDARY_SOLVER_MAX_ITER):
        next_eff = interpolate_efficiency(
            discharge_curve, delta_wh * eff / step_hours / 1000.0
        )
        if abs(next_eff - eff) < _BOUNDARY_SOLVER_TOL:
            eff = next_eff
            break
        eff = next_eff
    return delta_wh * eff / step_hours


def solve_boundary_fill_w(
    delta_wh: float,
    step_hours: float,
    charge_curve: EfficiencyCurve,
    seed_eff: float,
) -> float:
    """AC charge power that stores exactly ``delta_wh`` in the battery.

    Charging counterpart of :func:`solve_boundary_drain_w`; the transition is
    ``delta = power * hours * eff(power)``.
    """
    eff = seed_eff
    for _ in range(_BOUNDARY_SOLVER_MAX_ITER):
        next_eff = interpolate_efficiency(
            charge_curve, delta_wh / (step_hours * eff) / 1000.0
        )
        if abs(next_eff - eff) < _BOUNDARY_SOLVER_TOL:
            eff = next_eff
            break
        eff = next_eff
    return delta_wh / (step_hours * eff)


def calculate_step_cost(
    time_step_hours: float,
    soc_wh: float,
    action_w: float,  # positive = charge, negative = discharge
    grid_price: float,  # EUR/kWh buy price
    feed_in_price: float,  # EUR/kWh sell price
    pv_production_w: float,  # AC-side PV production in W
    consumption_w: float,
    charge_curve: EfficiencyCurve,
    discharge_curve: EfficiencyCurve,
    degradation_cost_per_kwh: float,  # EUR/kWh throughput
    battery_config: BatteryConfig,
    pv_dc_production_w: float = 0.0,  # DC-coupled PV production in W
    charge_eff: float | None = None,  # pre-interpolated from charge_curve
    discharge_eff: float | None = None,  # pre-interpolated from discharge_curve
    arbitrage_cost_per_kwh: float = 0.0,  # EUR/kWh commanded AC throughput
) -> float:
    """Calculate cost for a single time step.

    charge_eff / discharge_eff may be supplied pre-interpolated (hot-path
    optimisation: the DP evaluates a fixed action set against fixed curves, so
    the caller can interpolate once per action instead of once per state).
    They MUST come from the same curves passed as charge_curve/discharge_curve.

    Cost calculation with efficiency curves, degradation, and DC-coupled PV:

    1. Efficiency Effect (AC path):
       - action_w is the AC power setpoint (what the inverter is told to do)
       - charge_eff = interpolated from charge_curve at action power level
       - discharge_eff = interpolated from discharge_curve at action power level
       - Charging: grid draws the AC setpoint directly; losses are internal to
         the inverter, captured in the SoC transition (battery stores action_w * charge_eff)
       - Discharging: AC output = abs(action_w) (the setpoint); battery-side draw
         is action_w / discharge_eff (also captured in SoC transition)
       - Throughput for degradation: charging = action_w * charge_eff * dt / 1000 (Wh
         actually stored); discharging = abs(action_w) / discharge_eff * dt / 1000 (Wh drawn)

    2. DC-coupled PV:
       - PV panels connected directly to battery inverter DC bus
       - Charge efficiency ~97% (MPPT only, no AC conversion)
       - This PV power is "free" and doesn't pass through grid meter
       - The AC setpoint only controls AC-side exchange: DC MPPT charging
         continues both in idle mode AND on top of an active charge action,
         up to the available SoC headroom
       - Excess DC PV (when battery full or discharging) goes through inverter to AC

    3. Degradation:
       - Every kWh through the battery costs degradation
       - DC PV charging also counts for degradation
       - Prevents unnecessary cycles at small price differences

    3b. Arbitrage hurdle (arbitrage_cost_per_kwh):
       - Half of the user's min_price_spread, charged per kWh of COMMANDED AC
         throughput in either direction, so one full cycle carries the whole
         spread: 2 x degradation + min_price_spread — exactly the threshold the
         post-DP oscillation filter has always applied.
       - Passive DC PV charging is exempt: it happens whatever the setpoint is,
         so it is not an arbitrage decision and must not be discouraged.
       - Putting the hurdle here rather than only in the post-filter is what
         makes the DP solve the problem the user actually configured. Applied
         afterwards, the hurdle removed actions from an already-optimal
         schedule depending on whether a counterpart happened to fall inside a
         two-hour window, which cost a large share of the achievable value on
         quarter-hourly prices.
       - It is a decision hurdle, not money: reported costs and savings are
         recomputed with arbitrage_cost_per_kwh = 0.
       - Note this gates PV self-consumption too, not only grid-to-grid
         arbitrage: storing a kWh of PV surplus is commanded AC throughput like
         any other. That is intended — the round trip wears the battery either
         way — but it does mean min_price_spread is a "do not cycle below this
         margin" knob rather than a "do not trade below this margin" one.

    4. Grid capacity cap:
       - If battery_config.max_grid_power_kw > 0, EXPORT is capped here: excess
         PV that cannot be exported is treated as zero-revenue generation, which
         is what curtailment costs. Discharging past the cap is therefore
         self-limiting — it earns nothing and still pays degradation.
       - IMPORT is not capped here. A cap on the price of over-cap import makes
         that import free (the SoC rises, the cost does not), so the limit is
         enforced on the action set instead, via grid_cap_charge_limit_w().

    Args:
        time_step_hours: Duration of time step in hours
        soc_wh: Current state of charge in Wh
        action_w: Battery action in W (positive = charge, negative = discharge)
        grid_price: Grid buy price in EUR/kWh
        feed_in_price: Grid sell price in EUR/kWh
        pv_production_w: AC-side PV production in W (already inverted, clamped >= 0)
        consumption_w: Consumption in W
        charge_curve: Charge efficiency curve (power_kw, efficiency) pairs
        discharge_curve: Discharge efficiency curve (power_kw, efficiency) pairs
        degradation_cost_per_kwh: Degradation cost in EUR/kWh throughput
        battery_config: Battery configuration
        pv_dc_production_w: DC-coupled PV production in W (before inverter, clamped >= 0)

    Returns:
        Total cost in EUR for this time step
    """
    # Interpolate only when the caller did not pre-compute: the DP hot path
    # supplies both, and abs()/divide on every one of millions of calls is not free.
    if charge_eff is None or discharge_eff is None:
        action_kw = abs(action_w) / 1000.0
        if charge_eff is None:
            charge_eff = interpolate_efficiency(charge_curve, action_kw)
        if discharge_eff is None:
            discharge_eff = interpolate_efficiency(discharge_curve, action_kw)
    # Only read on the DC-coupled paths below; the MPPT path never goes through
    # the AC charger, so it does not use the charge curve.
    dc_eff = battery_config.pv_dc_efficiency

    # Clamp PV values: a faulty sensor should not appear as load
    pv_production_w = max(0.0, pv_production_w)
    pv_dc_production_w = max(0.0, pv_dc_production_w)

    # Handle DC-coupled PV
    # DC PV charges the battery directly at higher efficiency. The AC setpoint
    # only controls AC-side power exchange; DC MPPT charging continues
    # regardless of the setpoint. So passive DC PV charging happens both in
    # idle mode AND on top of an active AC charge action — an explicit charge
    # command never reduces the amount of DC PV the battery absorbs.
    dc_pv_excess_w = pv_dc_production_w  # DC PV not used by battery -> goes to AC
    # Throughput is tracked in two buckets: energy the AC setpoint commanded
    # (an arbitrage decision) and energy the DC MPPT absorbed passively (not a
    # decision at all). Degradation applies to both; the arbitrage hurdle only
    # to the commanded part.
    ac_throughput_kwh = 0.0
    passive_throughput_kwh = 0.0

    if action_w > 0:  # CHARGING
        # AC charging: grid draws the AC setpoint directly; losses are internal
        # to the inverter, captured in the SoC transition.
        grid_to_battery_w = action_w  # grid draws AC setpoint power
        ac_stored_wh = action_w * time_step_hours * charge_eff
        ac_throughput_kwh = ac_stored_wh / 1000  # actual stored Wh via AC

        if battery_config.pv_dc_coupled and pv_dc_production_w > 0:
            # DC MPPT charging continues on top of the AC charge, limited by
            # the headroom that remains after the AC-charged energy and by the
            # charge power the AC setpoint has not already used.
            max_soc_wh = battery_config.max_soc_kwh * 1000
            headroom_wh = max(0.0, max_soc_wh - soc_wh - ac_stored_wh)
            passive_charge_wh = passive_dc_charge_wh(
                pv_dc_production_w,
                dc_eff,
                time_step_hours,
                headroom_wh,
                charge_budget_wh(
                    battery_config.max_charge_power_kw, time_step_hours, ac_stored_wh
                ),
            )
            passive_charge_w = (
                passive_charge_wh / time_step_hours if time_step_hours > 0 else 0.0
            )
            dc_pv_consumed_w = passive_charge_w / dc_eff if dc_eff > 0 else 0.0
            dc_pv_excess_w = max(0.0, pv_dc_production_w - dc_pv_consumed_w)
            passive_throughput_kwh = passive_charge_wh / 1000

    elif action_w < 0:  # DISCHARGING
        # All DC PV excess goes to AC side when discharging
        dc_pv_excess_w = pv_dc_production_w

        # AC output = discharge setpoint (no multiplier needed)
        usable_power_w = abs(action_w)  # AC output = discharge setpoint
        grid_to_battery_w = -usable_power_w  # Negative = to home

        ac_throughput_kwh = (
            abs(action_w) * time_step_hours / discharge_eff / 1000
        )  # actual battery-drawn Wh

    else:  # IDLE (action_w == 0)
        grid_to_battery_w = 0.0
        if battery_config.pv_dc_coupled and pv_dc_production_w > 0:
            # DC-coupled inverters absorb DC PV into the battery passively even
            # when the AC setpoint is 0. Model this as free passive charging up
            # to the available SoC headroom and the battery's charge rating.
            max_soc_wh = battery_config.max_soc_kwh * 1000
            headroom_wh = max(0.0, max_soc_wh - soc_wh)
            passive_charge_wh = passive_dc_charge_wh(
                pv_dc_production_w,
                dc_eff,
                time_step_hours,
                headroom_wh,
                charge_budget_wh(
                    battery_config.max_charge_power_kw, time_step_hours, 0.0
                ),
            )
            passive_charge_w = (
                passive_charge_wh / time_step_hours if time_step_hours > 0 else 0.0
            )
            dc_pv_consumed_w = passive_charge_w / dc_eff if dc_eff > 0 else 0.0
            dc_pv_excess_w = max(0.0, pv_dc_production_w - dc_pv_consumed_w)
            passive_throughput_kwh = passive_charge_wh / 1000
        else:
            dc_pv_excess_w = pv_dc_production_w

    # DC PV excess converted to AC (through inverter, ~96% efficiency)
    dc_pv_to_ac_w = (
        dc_pv_excess_w * DC_TO_AC_INVERTER_EFFICIENCY if dc_pv_excess_w > 0 else 0.0
    )

    # Total AC-side PV = external AC PV + DC PV excess converted to AC
    total_ac_pv_w = pv_production_w + dc_pv_to_ac_w

    # Net grid exchange (positive = buy, negative = sell)
    net_grid_w = consumption_w - total_ac_pv_w + grid_to_battery_w

    # Grid capacity cap, export side only (0 = unlimited). PV that cannot be
    # exported is curtailed, which zero revenue models correctly. Import is
    # deliberately NOT clipped here: clipping it would make every watt above
    # the cap free while the SoC still rose, so the DP charged at full power
    # whenever the cap was binding. The import side is enforced on the action
    # set instead — see grid_cap_charge_limit_w().
    if battery_config.max_grid_power_kw > 0:
        net_grid_w = max(-battery_config.max_grid_power_kw * 1000, net_grid_w)

    # Grid costs/revenue
    energy_kwh = abs(net_grid_w) * time_step_hours / 1000
    if net_grid_w > 0:
        grid_cost = energy_kwh * grid_price  # Buying
    else:
        grid_cost = -energy_kwh * feed_in_price  # Selling (negative cost)

    # Degradation costs (all battery throughput, including passive DC PV charging)
    degradation_cost = (
        ac_throughput_kwh + passive_throughput_kwh
    ) * degradation_cost_per_kwh

    # Arbitrage hurdle on commanded throughput only (see 3b above)
    arbitrage_cost = ac_throughput_kwh * arbitrage_cost_per_kwh

    return grid_cost + degradation_cost + arbitrage_cost


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
    charge_eff_curve_override: EfficiencyCurve | None = None,
    discharge_eff_curve_override: EfficiencyCurve | None = None,
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
        charge_eff_curve_override: Override for the charge efficiency curve (all
            points scaled by the calibration correction factor). When provided,
            replaces the nominal curve from battery_config for SoC transitions
            ONLY. Economic costs (grid + degradation) always use the nominal
            curves so a charging-speed problem is not double-counted as extra
            energy cost or degradation.
        discharge_eff_curve_override: Override for the discharge efficiency curve.
            Same semantics as charge_eff_curve_override (transitions only; the
            points may exceed 1.0 after calibration scaling, which is safe
            because they never enter the cost model).

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

    # Select curves. Calibration overrides replace the nominal curves for SoC
    # TRANSITIONS only; the cost model always uses the nominal curves so a
    # charging/discharging-speed problem is not double-counted as extra energy
    # cost or degradation (override points may even exceed 1.0).
    # Reject override curves carrying a non-positive efficiency: those divide by
    # zero in the discharge transition and in _rebuild_schedule.
    if charge_eff_curve_override is not None and not all(
        e > 0.0 for _, e in charge_eff_curve_override
    ):
        charge_eff_curve_override = None
    if discharge_eff_curve_override is not None and not all(
        e > 0.0 for _, e in discharge_eff_curve_override
    ):
        discharge_eff_curve_override = None

    charge_curve = (
        charge_eff_curve_override
        if charge_eff_curve_override is not None
        else battery_config.charge_efficiency_curve_parsed
    )
    discharge_curve = (
        discharge_eff_curve_override
        if discharge_eff_curve_override is not None
        else battery_config.discharge_efficiency_curve_parsed
    )
    cost_charge_curve = battery_config.charge_efficiency_curve_parsed
    cost_discharge_curve = battery_config.discharge_efficiency_curve_parsed

    # Arbitrage hurdle: half the configured spread per direction, so a full
    # cycle carries 2 x degradation + min_price_spread.  This is the same
    # threshold the post-DP oscillation filter applies, but inside the
    # objective, so the DP returns the optimum UNDER the user's hurdle instead
    # of an optimum-without-hurdle that a window heuristic then thins out.
    # See calculate_step_cost section 3b.
    arbitrage_cost_per_kwh = max(0.0, min_price_spread) / 2.0

    # Discretize SoC space.
    min_step_hours = min(step_durations_hours[:n_steps])

    # Round to nearest Wh to avoid floating-point drift (e.g. 2.12 * 0.1 * 1000
    # = 212.00000000000003).  Without rounding, soc_states[0] may be computed as
    # 912.0 (losing the tiny fractional part) while min_soc_wh stays at
    # 212.00000000000003, so the action that would exactly reach min_soc passes
    # the boundary check (212.0 < 212.00000000000003) and gets skipped.
    min_soc_wh = round(battery_config.min_soc_kwh * 1000)
    max_soc_wh = round(battery_config.max_soc_kwh * 1000)

    # SoC resolution: see compute_soc_resolution_wh().  Capping the state count
    # keeps the resolution at or below 0.1% of usable capacity — well under SoC
    # sensor accuracy — while leaving batteries up to 10 kWh of usable range on
    # the exact 10 Wh grid.
    #
    # The resolution is deliberately NOT inflated by the power step.  It used to
    # be max(SOC_RESOLUTION_WH, POWER_STEP_W * min_step_hours * sqrt_rte) to
    # ensure the minimum power action always moved at least one state; with
    # hourly prices that inflated to ~87 Wh (only 22 states for a 2 kWh
    # battery), causing the DP to coarsely map post-discharge SoC and
    # systematically undervalue concentrating discharge at the peak-price hour.
    # The per-action sub-resolution guard (new_soc_idx == s_idx → skip) already
    # handles steps where a small action cannot cross a state boundary.
    soc_resolution_wh = compute_soc_resolution_wh(min_soc_wh, max_soc_wh)

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

    n_soc_states = int(round((max_soc_wh - min_soc_wh) / soc_resolution_wh)) + 1
    if n_soc_states > 1:
        # Make the grid divide the usable range exactly, so the top state IS
        # max_soc_wh.  With a fixed resolution a range that is not a whole
        # multiple of it left the last few Wh unreachable, and the fill-to-max
        # boundary action then charged to a SoC the state it was credited to did
        # not represent.  Shrinking the step instead of adding a state keeps the
        # MAX_SOC_STATES budget intact.
        soc_resolution_wh = (max_soc_wh - min_soc_wh) / (n_soc_states - 1)
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
    # The shadow price is still returned to the caller and used by hybrid
    # mode as the charge/discharge switching threshold — it is just not used
    # to initialise V[T].
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
        # Clamp at 0: a negative feed-in tail must not give stored energy a
        # negative terminal value. The horizon end is artificial — the battery
        # is never forced to sell at a loss, so holding energy is worth at
        # least zero. Without the clamp the DP dumps energy before the horizon
        # ends purely to escape the artificial penalty.
        terminal_price = max(0.0, min(feed_in_forecast[-1], avg_tail))
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

    # Pre-compute per-action efficiencies once: the action set and curves are
    # fixed for the whole horizon, so interpolating inside the t × SoC × action
    # loops (millions of iterations) would repeat the exact same lookups.
    # Transition efficiencies come from the (possibly overridden) curves;
    # cost efficiencies always from the nominal curves.
    # Stored as lists parallel to `actions` and indexed positionally: the inner
    # loop runs millions of times and float-keyed dict lookups are measurably
    # slower than list indexing.
    trans_charge_eff = [
        interpolate_efficiency(charge_curve, abs(a) / 1000.0) for a in actions
    ]
    trans_discharge_eff = [
        interpolate_efficiency(discharge_curve, abs(a) / 1000.0) for a in actions
    ]
    cost_charge_eff = [
        interpolate_efficiency(cost_charge_curve, abs(a) / 1000.0) for a in actions
    ]
    cost_discharge_eff = [
        interpolate_efficiency(cost_discharge_curve, abs(a) / 1000.0) for a in actions
    ]

    # Boundary actions ("use exactly the residual capacity") need the power that
    # lands on min/max SoC, but that power depends on the efficiency at that same
    # power.  Solve the fixed point by iteration, seeded with the representative
    # scalar.  Two passes are enough: the curves are smooth and the residual
    # capacities involved are small.  A single zero-power scalar would be wrong
    # by tens of percentage points on a steep curve, because boundary powers land
    # in exactly the steep part-load region.
    _chg_eff_seed = representative_efficiency(
        charge_curve, battery_config.max_charge_power_kw
    )
    _dis_eff_seed = representative_efficiency(
        discharge_curve, battery_config.max_discharge_power_kw
    )

    def _solve_drain_w(delta_wh: float, step_hours: float) -> float:
        return solve_boundary_drain_w(
            delta_wh, step_hours, discharge_curve, _dis_eff_seed
        )

    def _solve_fill_w(delta_wh: float, step_hours: float) -> float:
        return solve_boundary_fill_w(delta_wh, step_hours, charge_curve, _chg_eff_seed)

    # Pre-compute SoC-dependent power limits for every SoC state.
    # For batteries without derating these equal max_charge_w / max_discharge_w
    # and the guards below never fire, so there is no performance regression.
    soc_max_charge_w = [
        battery_config.max_charge_at_soc(s_wh / 1000) * 1000 for s_wh in soc_states
    ]
    soc_max_discharge_w = [
        battery_config.max_discharge_at_soc(s_wh / 1000) * 1000 for s_wh in soc_states
    ]

    # Hoisted loop invariants. The inner loop runs t x SoC x actions times —
    # millions of iterations — so attribute lookups and the nearest-state helper
    # call are worth taking out of it. The index arithmetic below is the exact
    # body of _find_nearest_soc_idx, kept identical so results are unchanged.
    dc_coupled = battery_config.pv_dc_coupled
    dc_eff_cfg = battery_config.pv_dc_efficiency
    soc_state0 = soc_states[0]
    soc_state_step = soc_states[1] - soc_states[0] if n_soc_states > 1 else 1.0
    last_soc_idx = n_soc_states - 1

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
        V_next = V[t + 1]

        # Step cost per action, precomputed once for this step. The cost only
        # depends on the SoC through the DC-PV headroom term, so:
        #  - no DC PV producing  -> valid for every SoC (limit = +inf)
        #  - discharging         -> valid for every SoC (branch ignores soc_wh)
        #  - charging / idle with DC PV -> valid while the passive charge is not
        #    clipped by headroom, i.e. below cache_soc_limit; only the handful of
        #    states near full SoC fall through to a real call.
        # Without this the identical cost is recomputed once per SoC state, i.e.
        # up to 1001 times per action per step.
        dc_active = dc_coupled and pv_dc_w > 0
        passive_full_wh = pv_dc_w * dc_eff_cfg * time_step_hours if dc_active else 0.0
        # Largest AC charge setpoint that keeps grid import within the physical
        # connection (inf when no cap is configured). Applied to the action set
        # rather than to the cost — see grid_cap_charge_limit_w().
        step_charge_cap_w = grid_cap_charge_limit_w(
            battery_config.max_grid_power_kw, consumption_w, pv_w
        )
        # Passive DC charge per action, before the SoC headroom is applied: it
        # depends on the action only through the charge-power budget the AC
        # setpoint leaves over, so it is the same for every SoC state. Hoisting
        # it also removes a multiplication from the innermost loop.
        trans_passive_wh: list[float] = [0.0] * len(actions)
        if dc_active:
            for a_idx, action_w in enumerate(actions):
                ac_stored_wh = (
                    action_w * time_step_hours * trans_charge_eff[a_idx]
                    if action_w > 0
                    else 0.0
                )
                trans_passive_wh[a_idx] = min(
                    passive_full_wh,
                    charge_budget_wh(
                        battery_config.max_charge_power_kw,
                        time_step_hours,
                        ac_stored_wh,
                    ),
                )
        step_cost_cache: list[float] = [0.0] * len(actions)
        cache_soc_limit: list[float] = [INF] * len(actions)
        for a_idx, action_w in enumerate(actions):
            probe_soc_wh = 0.0
            if action_w > step_charge_cap_w:
                # Never selectable below, so its cost is never read. Mark the
                # cache entry unusable so a future reader recomputes instead of
                # silently picking up the placeholder.
                cache_soc_limit[a_idx] = -INF
                continue
            if dc_active and action_w >= 0:
                # Below this SoC the headroom never clips the passive charge, so
                # one evaluation covers all of them. Probe at min_soc_wh, which
                # is always inside that range. The charge-power budget is
                # SoC-independent by construction (see charge_budget_wh), so it
                # only shrinks the passive amount, never the validity range.
                ac_stored_wh = (
                    action_w * time_step_hours * cost_charge_eff[a_idx]
                    if action_w > 0
                    else 0.0
                )
                passive_wh = min(
                    passive_full_wh,
                    charge_budget_wh(
                        battery_config.max_charge_power_kw,
                        time_step_hours,
                        ac_stored_wh,
                    ),
                )
                cache_soc_limit[a_idx] = max_soc_wh - ac_stored_wh - passive_wh
                probe_soc_wh = float(min_soc_wh)
                if probe_soc_wh > cache_soc_limit[a_idx]:
                    cache_soc_limit[a_idx] = -INF  # never usable; always recompute
                    continue
            step_cost_cache[a_idx] = calculate_step_cost(
                time_step_hours=time_step_hours,
                soc_wh=probe_soc_wh,
                action_w=action_w,
                grid_price=grid_price,
                feed_in_price=feed_in_price,
                pv_production_w=pv_w,
                consumption_w=consumption_w,
                charge_curve=cost_charge_curve,
                discharge_curve=cost_discharge_curve,
                degradation_cost_per_kwh=degradation_cost_per_kwh,
                battery_config=battery_config,
                pv_dc_production_w=pv_dc_w,
                charge_eff=cost_charge_eff[a_idx],
                discharge_eff=cost_discharge_eff[a_idx],
                arbitrage_cost_per_kwh=arbitrage_cost_per_kwh,
            )

        for s_idx, soc_wh in enumerate(soc_states):
            best_cost = INF
            best_action = 0.0
            # Charging is bounded by the battery (SoC-dependent derating) and by
            # the grid connection; whichever binds first wins.
            max_chg_w = min(soc_max_charge_w[s_idx], step_charge_cap_w)
            max_dis_w = soc_max_discharge_w[s_idx]

            for a_idx, action_w in enumerate(actions):
                # SoC transition: action_w is the explicit AC setpoint. For
                # DC-coupled systems the MPPT charger passively charges the
                # battery from DC PV up to available headroom regardless of the
                # AC setpoint — both in idle and on top of an active charge.
                # Efficiency losses are on the grid/AC side and handled in
                # calculate_step_cost.
                if action_w > 0:
                    if action_w > max_chg_w:
                        continue
                    _charge_eff = trans_charge_eff[a_idx]
                    energy_change_wh = action_w * time_step_hours * _charge_eff
                    new_soc_wh = soc_wh + energy_change_wh
                    if new_soc_wh > max_soc_wh:
                        continue
                    if dc_active:
                        headroom_wh = max(0.0, max_soc_wh - new_soc_wh)
                        new_soc_wh += min(trans_passive_wh[a_idx], headroom_wh)
                elif action_w < 0:
                    if -action_w > max_dis_w:
                        continue
                    # Discharge: action_w is AC setpoint → battery must supply
                    # abs(action_w) / discharge_eff from its DC side.
                    _discharge_eff = trans_discharge_eff[a_idx]
                    energy_change_wh = abs(action_w) * time_step_hours / _discharge_eff
                    new_soc_wh = soc_wh - energy_change_wh
                    if new_soc_wh < min_soc_wh:
                        continue
                else:
                    # Idle: no explicit AC charge/discharge.
                    # For DC-coupled systems, DC PV passively charges the battery.
                    if dc_active:
                        headroom_wh = max(0.0, max_soc_wh - soc_wh)
                        new_soc_wh = soc_wh + min(trans_passive_wh[a_idx], headroom_wh)
                    else:
                        new_soc_wh = soc_wh

                # Nearest SoC state for the next step (inlined
                # _find_nearest_soc_idx; identical arithmetic)
                new_soc_idx = round((new_soc_wh - soc_state0) / soc_state_step)
                if new_soc_idx < 0:
                    new_soc_idx = 0
                elif new_soc_idx > last_soc_idx:
                    new_soc_idx = last_soc_idx

                # Skip sub-resolution AC actions: a non-zero action that doesn't
                # cross a SoC state boundary appears "free" to the DP (no future
                # value change) while still incurring real RTE losses, producing
                # oscillating micro-charge/discharge artifacts.
                # Idle (action_w == 0) is exempt — passive DC PV may still change
                # the SoC bin, and even if it doesn't, the cost is real.
                if action_w != 0 and new_soc_idx == s_idx:
                    continue

                # Immediate cost (always on the nominal curves). Taken from the
                # per-step cache unless this action's cost depends on the SoC.
                if soc_wh <= cache_soc_limit[a_idx]:
                    step_cost = step_cost_cache[a_idx]
                else:
                    step_cost = calculate_step_cost(
                        time_step_hours=time_step_hours,
                        soc_wh=soc_wh,
                        action_w=action_w,
                        grid_price=grid_price,
                        feed_in_price=feed_in_price,
                        pv_production_w=pv_w,
                        consumption_w=consumption_w,
                        charge_curve=cost_charge_curve,
                        discharge_curve=cost_discharge_curve,
                        degradation_cost_per_kwh=degradation_cost_per_kwh,
                        battery_config=battery_config,
                        pv_dc_production_w=pv_dc_w,
                        charge_eff=cost_charge_eff[a_idx],
                        discharge_eff=cost_discharge_eff[a_idx],
                        arbitrage_cost_per_kwh=arbitrage_cost_per_kwh,
                    )

                # Total cost = immediate + future
                total_cost = step_cost + V_next[new_soc_idx]

                if total_cost < best_cost:
                    best_cost = total_cost
                    best_action = action_w

            # Boundary actions: exact power to reach min/max SoC in this step.
            # new_soc_idx is known directly (0 or n_soc_states-1), avoiding the
            # floating-point round-trip through the energy formula.
            # Power estimate uses the zero-power transition scalar (hoisted
            # above the loops; second-order error).
            if soc_wh > min_soc_wh:
                drain_w = _solve_drain_w(soc_wh - min_soc_wh, time_step_hours)
                if 0 < drain_w <= max_dis_w:
                    step_cost = calculate_step_cost(
                        time_step_hours=time_step_hours,
                        soc_wh=soc_wh,
                        action_w=-drain_w,
                        grid_price=grid_price,
                        feed_in_price=feed_in_price,
                        pv_production_w=pv_w,
                        consumption_w=consumption_w,
                        charge_curve=cost_charge_curve,
                        discharge_curve=cost_discharge_curve,
                        degradation_cost_per_kwh=degradation_cost_per_kwh,
                        battery_config=battery_config,
                        pv_dc_production_w=pv_dc_w,
                        arbitrage_cost_per_kwh=arbitrage_cost_per_kwh,
                    )
                    total_cost = step_cost + V_next[0]
                    if total_cost < best_cost:
                        best_cost = total_cost
                        best_action = -drain_w
            if soc_wh < max_soc_wh:
                fill_w = _solve_fill_w(max_soc_wh - soc_wh, time_step_hours)
                if 0 < fill_w <= max_chg_w:
                    step_cost = calculate_step_cost(
                        time_step_hours=time_step_hours,
                        soc_wh=soc_wh,
                        action_w=fill_w,
                        grid_price=grid_price,
                        feed_in_price=feed_in_price,
                        pv_production_w=pv_w,
                        consumption_w=consumption_w,
                        charge_curve=cost_charge_curve,
                        discharge_curve=cost_discharge_curve,
                        degradation_cost_per_kwh=degradation_cost_per_kwh,
                        battery_config=battery_config,
                        pv_dc_production_w=pv_dc_w,
                        arbitrage_cost_per_kwh=arbitrage_cost_per_kwh,
                    )
                    total_cost = step_cost + V_next[last_soc_idx]
                    if total_cost < best_cost:
                        best_cost = total_cost
                        best_action = fill_w

            V[t][s_idx] = best_cost
            policy[t][s_idx] = best_action

    # Find current SoC index in the DP state space (needed for the shadow
    # price).  Uses the continuous SoC, like the forward pass below: truncating
    # to whole Wh first could snap to the neighbouring state near a boundary.
    current_soc_idx = _find_nearest_soc_idx(current_soc_kwh * 1000.0, soc_states)

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
        # Same two bounds as the backward pass: battery derating and the grid
        # connection. Keeping them identical is what makes the forward pass
        # re-evaluate the very action set the V-table was built from.
        step_charge_cap_w = grid_cap_charge_limit_w(
            battery_config.max_grid_power_kw, consumption_w, pv_w
        )
        max_chg_w = min(
            battery_config.max_charge_at_soc(current_soc / 1000) * 1000,
            step_charge_cap_w,
        )
        max_dis_w = battery_config.max_discharge_at_soc(current_soc / 1000) * 1000

        best_cost = INF
        best_action = 0.0
        best_new_soc = current_soc

        for a_idx, action_w in enumerate(actions):
            if action_w > 0:
                if action_w > max_chg_w:
                    continue
                _ce = trans_charge_eff[a_idx]
                new_soc_wh = current_soc + action_w * time_step_hours * _ce
                if new_soc_wh > max_soc_wh:
                    continue
                if battery_config.pv_dc_coupled and pv_dc_w > 0:
                    headroom_wh = max(0.0, float(max_soc_wh) - new_soc_wh)
                    new_soc_wh += passive_dc_charge_wh(
                        pv_dc_w,
                        battery_config.pv_dc_efficiency,
                        time_step_hours,
                        headroom_wh,
                        charge_budget_wh(
                            battery_config.max_charge_power_kw,
                            time_step_hours,
                            action_w * time_step_hours * _ce,
                        ),
                    )
            elif action_w < 0:
                if -action_w > max_dis_w:
                    continue
                _de = trans_discharge_eff[a_idx]
                new_soc_wh = current_soc - abs(action_w) * time_step_hours / _de
                if new_soc_wh < min_soc_wh:
                    continue
            else:
                if battery_config.pv_dc_coupled and pv_dc_w > 0:
                    headroom_wh = max(0.0, float(max_soc_wh) - current_soc)
                    new_soc_wh = current_soc + passive_dc_charge_wh(
                        pv_dc_w,
                        battery_config.pv_dc_efficiency,
                        time_step_hours,
                        headroom_wh,
                        charge_budget_wh(
                            battery_config.max_charge_power_kw, time_step_hours, 0.0
                        ),
                    )
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
                charge_curve=cost_charge_curve,
                discharge_curve=cost_discharge_curve,
                degradation_cost_per_kwh=degradation_cost_per_kwh,
                battery_config=battery_config,
                pv_dc_production_w=pv_dc_w,
                charge_eff=cost_charge_eff[a_idx],
                discharge_eff=cost_discharge_eff[a_idx],
                arbitrage_cost_per_kwh=arbitrage_cost_per_kwh,
            )
            total_cost = step_cost + V[t + 1][new_soc_idx]
            if total_cost < best_cost:
                best_cost = total_cost
                best_action = action_w
                best_new_soc = new_soc_wh

        # Boundary actions: exact power to drain to min or fill to max SoC
        if current_soc > min_soc_wh:
            drain_w = _solve_drain_w(current_soc - min_soc_wh, time_step_hours)
            if 0 < drain_w <= max_dis_w:
                step_cost = calculate_step_cost(
                    time_step_hours=time_step_hours,
                    soc_wh=current_soc,
                    action_w=-drain_w,
                    grid_price=grid_price,
                    feed_in_price=feed_in_price,
                    pv_production_w=pv_w,
                    consumption_w=consumption_w,
                    charge_curve=cost_charge_curve,
                    discharge_curve=cost_discharge_curve,
                    degradation_cost_per_kwh=degradation_cost_per_kwh,
                    battery_config=battery_config,
                    pv_dc_production_w=pv_dc_w,
                    arbitrage_cost_per_kwh=arbitrage_cost_per_kwh,
                )
                total_cost = step_cost + V[t + 1][0]
                if total_cost < best_cost:
                    best_cost = total_cost
                    best_action = -drain_w
                    best_new_soc = float(min_soc_wh)

        if current_soc < max_soc_wh:
            fill_w = _solve_fill_w(max_soc_wh - current_soc, time_step_hours)
            if 0 < fill_w <= max_chg_w:
                step_cost = calculate_step_cost(
                    time_step_hours=time_step_hours,
                    soc_wh=current_soc,
                    action_w=fill_w,
                    grid_price=grid_price,
                    feed_in_price=feed_in_price,
                    pv_production_w=pv_w,
                    consumption_w=consumption_w,
                    charge_curve=cost_charge_curve,
                    discharge_curve=cost_discharge_curve,
                    degradation_cost_per_kwh=degradation_cost_per_kwh,
                    battery_config=battery_config,
                    pv_dc_production_w=pv_dc_w,
                    arbitrage_cost_per_kwh=arbitrage_cost_per_kwh,
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

    # Keep the unfiltered schedule so raw_total_cost can be priced the same way
    # as total_cost. Reading V[0] instead mixed two things that are no longer
    # comparable: V is the DP objective, which includes the arbitrage hurdle
    # and is evaluated at a snapped SoC state, while total_cost is real money
    # for the continuous-SoC schedule actually returned.
    raw_power_schedule_kw = list(power_schedule_kw)
    raw_soc_schedule_kwh = list(soc_schedule_kwh)

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

    def _price_schedule(power_kw: list[float], soc_kwh: list[float]) -> float:
        """Real money for one candidate schedule (no arbitrage hurdle)."""
        return _calculate_schedule_total_cost(
            battery_config=battery_config,
            power_schedule_kw=power_kw,
            soc_schedule_kwh=soc_kwh,
            price_forecast=price_forecast[:n_steps],
            feed_in_forecast=(
                feed_in_forecast[:n_steps]
                if feed_in_forecast
                else price_forecast[:n_steps]
            ),
            pv_forecast=pv_forecast[:n_steps],
            consumption_forecast=consumption_forecast[:n_steps],
            step_durations_hours=step_durations_hours[:n_steps],
            degradation_cost_per_kwh=degradation_cost_per_kwh,
            terminal_price=terminal_price,
            pv_dc_forecast=pv_dc_forecast[:n_steps],
        )

    raw_total_cost = _price_schedule(raw_power_schedule_kw, raw_soc_schedule_kwh)

    # Post-process: remove unprofitable oscillations
    osc_power_kw, osc_mode_schedule, osc_soc_kwh = _filter_oscillations(
        power_schedule_kw=power_schedule_kw,
        mode_schedule=mode_schedule,
        initial_soc_kwh=soc_schedule_kwh[0],
        price_forecast=price_forecast[:n_steps],
        min_price_spread=min_price_spread,
        degradation_cost_per_kwh=degradation_cost_per_kwh,
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
        charge_curve=charge_curve,
        discharge_curve=discharge_curve,
        cost_charge_curve=cost_charge_curve,
        cost_discharge_curve=cost_discharge_curve,
        max_charge_power_kw=battery_config.max_charge_power_kw,
        max_discharge_power_kw=battery_config.max_discharge_power_kw,
    )

    # Accept the filter's verdict only when it does not cost money.
    #
    # Since the arbitrage hurdle moved into the DP objective, the DP already
    # returns the optimum under the user's own threshold, and the filter is a
    # second opinion formed from a much cruder proxy: it prices every discharged
    # watt above the instantaneous residual load at the feed-in price, ignores
    # the terminal value of stored energy, and pairs steps by a fixed lookahead
    # window instead of by the SoC trajectory that actually links them. Where the
    # two disagree the DP is right, because it evaluates the real cost model.
    # Measured on simulated days this guard was worth ~3 % of achievable savings
    # on quarter-hourly prices and ~0.1 % on hourly ones — the finer the price
    # resolution, the more the window heuristic misfires.
    #
    # The filter still earns its keep where it agrees with the cost model: it
    # removes churn the DP is indifferent about, which is real wear the cost
    # model prices only through degradation.
    osc_total_cost = _price_schedule(osc_power_kw, osc_soc_kwh)
    if osc_total_cost <= raw_total_cost:
        power_schedule_kw, mode_schedule, soc_schedule_kwh = (
            osc_power_kw,
            osc_mode_schedule,
            osc_soc_kwh,
        )
    else:
        _LOGGER.debug(
            "Oscillation filter rejected: it would cost %.4f EUR "
            "(%.4f -> %.4f); keeping the DP schedule",
            osc_total_cost - raw_total_cost,
            raw_total_cost,
            osc_total_cost,
        )

    # Post-process: suppress micro-cycles (P5.1)
    power_schedule_kw, mode_schedule, soc_schedule_kwh = _filter_micro_cycles(
        power_schedule_kw=power_schedule_kw,
        mode_schedule=mode_schedule,
        initial_soc_kwh=soc_schedule_kwh[0],
        step_durations_hours=step_durations_hours[:n_steps],
        min_soc_kwh=battery_config.min_soc_kwh,
        max_soc_kwh=battery_config.max_soc_kwh,
        pv_dc_forecast=pv_dc_forecast[:n_steps] if pv_dc_forecast else None,
        pv_dc_coupled=battery_config.pv_dc_coupled,
        pv_dc_efficiency=battery_config.pv_dc_efficiency,
        min_cycle_kwh=MIN_CYCLE_KWH,
        charge_curve=charge_curve,
        discharge_curve=discharge_curve,
        max_charge_power_kw=battery_config.max_charge_power_kw,
    )

    # Shadow price: marginal value of 1 kWh stored at t=0, current SoC.
    # Computed after post-processing filters so it is consistent with the
    # filtered schedule presented to the caller. V[0] is not modified by the
    # filters (backward-pass values are stable), but placing the computation
    # here makes the intent clear: shadow price belongs to the filtered world.
    #   λ = -dV/dSoC = (V[s-1] - V[s+1]) / (2 * ΔSoC_kwh)
    # V is cost (lower is better); more energy lowers cost → gradient negative
    # → shadow price positive.
    # V carries the arbitrage hurdle, so λ is the marginal value of stored
    # energy under the user's own hurdle — the same economics the schedule was
    # planned with, which is what the hybrid-mode thresholds compare against.
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

    # raw_total_cost was priced before the filters ran (same real-money basis as
    # total_cost below), so raw_savings - savings is the euro price of the
    # post-processing that survived the cost guard.
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
    total_cost = _price_schedule(power_schedule_kw, soc_schedule_kwh)
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
    step_durations_hours: list[float],
    min_soc_kwh: float,
    max_soc_kwh: float,
    charge_curve: EfficiencyCurve,
    discharge_curve: EfficiencyCurve,
    pv_forecast: list[float] | None = None,
    consumption_forecast: list[float] | None = None,
    feed_in_forecast: list[float] | None = None,
    oscillation_window_hours: float = 2.0,
    pv_dc_forecast: list[float] | None = None,
    pv_dc_coupled: bool = False,
    pv_dc_efficiency: float = 0.97,
    cost_charge_curve: EfficiencyCurve | None = None,
    cost_discharge_curve: EfficiencyCurve | None = None,
    max_charge_power_kw: float = 0.0,
    max_discharge_power_kw: float = 0.0,
) -> tuple[list[float], list[str], list[float]]:
    """Filter out unprofitable oscillations from the schedule.

    charge_curve/discharge_curve are the (possibly calibration-overridden)
    transition curves used to rebuild the SoC schedule.  The profitability
    threshold is economic and uses cost_charge_curve/cost_discharge_curve
    (the nominal curves); they default to the transition curves when absent.

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

    # Reduce the curves to a representative operating efficiency for the
    # arbitrage threshold.  Sampling at zero power instead would take the worst
    # point of a realistic (part-load-limited) curve and inflate the threshold.
    if cost_charge_curve is None:
        cost_charge_curve = charge_curve
    if cost_discharge_curve is None:
        cost_discharge_curve = discharge_curve
    _rte = representative_efficiency(
        cost_charge_curve, max_charge_power_kw
    ) * representative_efficiency(cost_discharge_curve, max_discharge_power_kw)
    sqrt_rte = math.sqrt(_rte)
    filtered_power = list(power_schedule_kw)
    filtered_mode = list(mode_schedule)
    # Minimum profitable price spread needed for arbitrage
    # P_discharge * sqrt(rte) > P_charge / sqrt(rte) + 2 * degradation + min_spread
    # => P_discharge > P_charge / rte + (2 * degradation + min_spread) / sqrt(rte)
    min_arbitrage_spread = (2 * degradation_cost_per_kwh + min_price_spread) / sqrt_rte

    def get_charge_cost(timestep: int, charge_power_kw: float) -> float:
        """Get the marginal cost of charging for the actual commanded power.

        The commanded power is the AC setpoint only: passive DC PV charging
        happens regardless of the setpoint (also at idle), so it is never part
        of the commanded power and needs no deduction here.
        """
        if (
            charge_power_kw <= 0
            or not pv_forecast
            or not consumption_forecast
            or not feed_in_forecast
        ):
            return price_forecast[timestep]

        pv_surplus_kw = max(0.0, pv_forecast[timestep] - consumption_forecast[timestep])
        from_pv_kw = min(charge_power_kw, pv_surplus_kw)
        from_grid_kw = max(0.0, charge_power_kw - from_pv_kw)
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
                        effective_spread = discharge_price - charge_cost / _rte
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
                        effective_spread = discharge_price - charge_cost / _rte
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
        charge_curve=charge_curve,
        discharge_curve=discharge_curve,
        pv_dc_forecast=pv_dc_forecast,
        pv_dc_coupled=pv_dc_coupled,
        pv_dc_efficiency=pv_dc_efficiency,
        max_charge_power_kw=max_charge_power_kw,
    )


def _filter_micro_cycles(
    power_schedule_kw: list[float],
    mode_schedule: list[str],
    initial_soc_kwh: float,
    step_durations_hours: list[float],
    min_soc_kwh: float,
    max_soc_kwh: float,
    charge_curve: EfficiencyCurve,
    discharge_curve: EfficiencyCurve,
    pv_dc_forecast: list[float] | None = None,
    pv_dc_coupled: bool = False,
    pv_dc_efficiency: float = 0.97,
    min_cycle_kwh: float = 0.2,
    max_charge_power_kw: float = 0.0,
) -> tuple[list[float], list[str], list[float]]:
    """Filter out micro-cycles whose total energy is below min_cycle_kwh.

    Charge or discharge segments that move less energy than min_cycle_kwh have
    disproportionately high wear per kWh of useful storage — the part of ageing
    that a per-kWh degradation price cannot express, because it is per cycle
    rather than per throughput.  Replacing them with idle preserves battery
    lifespan.

    Unlike the oscillation filter this is deliberately NOT gated on cost: the
    wear it avoids is real but unpriced, so a cost guard would disable it
    entirely.  It is not free, though — measured over simulated quarter-hourly
    days it suppresses a handful of steps per day and costs on the order of 2 %
    of achievable savings.  Raise MIN_CYCLE_KWH to trade more savings for less
    wear, or lower it for the reverse.

    Block energy is measured on the BATTERY side (stored when charging, drawn
    when discharging), which is the quantity that actually wears the cells and
    the same one degradation is priced on.  Measuring the AC setpoint instead
    misjudged blocks near the threshold by the efficiency factor, in opposite
    directions for the two directions.

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

    # Step 0 is artificially shortened to align with the current price-period
    # boundary, and can be as little as one minute. Measuring the block on that
    # duration judged an action by when the optimizer happened to run rather
    # than by its economics, so an isolated first step was suppressed purely for
    # starting late in a period. Size it on the reference (full) interval
    # instead — the same correction the oscillation filter already applies.
    ref_step_h = (
        step_durations_hours[1]
        if len(step_durations_hours) > 1
        else (step_durations_hours[0] if step_durations_hours else 0.25)
    )

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
            if j == 0:
                step_h = ref_step_h
            elif j < len(step_durations_hours):
                step_h = step_durations_hours[j]
            else:
                step_h = step_durations_hours[-1]
            # Battery-side energy: what the cells actually see.
            power_kw = abs(filtered_power[j])
            if current_dir == ACTION_CHARGING:
                total_energy_kwh += (
                    power_kw * step_h * interpolate_efficiency(charge_curve, power_kw)
                )
            else:
                eff = interpolate_efficiency(discharge_curve, power_kw)
                total_energy_kwh += power_kw * step_h / eff if eff > 0 else 0.0
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
                charge_curve=charge_curve,
                discharge_curve=discharge_curve,
                pv_dc_forecast=pv_dc_forecast,
                pv_dc_coupled=pv_dc_coupled,
                pv_dc_efficiency=pv_dc_efficiency,
                max_charge_power_kw=max_charge_power_kw,
            )[2],
        )

    return _rebuild_schedule(
        power_schedule_kw=filtered_power,
        step_durations_hours=step_durations_hours,
        initial_soc_kwh=initial_soc_kwh,
        min_soc_kwh=min_soc_kwh,
        max_soc_kwh=max_soc_kwh,
        charge_curve=charge_curve,
        discharge_curve=discharge_curve,
        pv_dc_forecast=pv_dc_forecast,
        pv_dc_coupled=pv_dc_coupled,
        pv_dc_efficiency=pv_dc_efficiency,
        max_charge_power_kw=max_charge_power_kw,
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

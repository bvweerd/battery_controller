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


EfficiencyCurve = list[tuple[float, float]]  # [(power_kw, efficiency), ...]


def _interpolate_efficiency(curve: EfficiencyCurve, power_kw: float) -> float:
    """Linear interpolation on efficiency curve; flat clamping beyond range."""
    if not curve:
        return 1.0
    if len(curve) == 1 or power_kw <= curve[0][0]:
        return curve[0][1]
    if power_kw >= curve[-1][0]:
        return curve[-1][1]
    for i in range(len(curve) - 1):
        p0, e0 = curve[i]
        p1, e1 = curve[i + 1]
        if p0 <= power_kw <= p1:
            t = (power_kw - p0) / (p1 - p0)
            return e0 + t * (e1 - e0)
    return curve[-1][1]


def _flat_curve(eff: float, max_kw: float) -> EfficiencyCurve:
    """Build a flat (power-independent) efficiency curve."""
    return [(0.0, eff), (max_kw, eff)]


# 10 load points from 5% to 95% of nominal power (mirrors efficiency_curve.py).
_REPRESENTATIVE_LOAD_POINTS = tuple(0.05 + 0.10 * i for i in range(10))


def _representative_efficiency(curve: EfficiencyCurve, max_power_kw: float) -> float:
    """Reduce a curve to one scalar describing typical operating efficiency.

    Sampling at zero power instead would pick the single worst point of a
    realistic curve, inflating every threshold derived from it.
    """
    if not curve:
        return 1.0
    if max_power_kw <= 0:
        return _interpolate_efficiency(curve, 0.0)
    return sum(
        _interpolate_efficiency(curve, f * max_power_kw)
        for f in _REPRESENTATIVE_LOAD_POINTS
    ) / len(_REPRESENTATIVE_LOAD_POINTS)


# Mirrors DEFAULT_MIN_PRICE_SPREAD in const.py: the arbitrage hurdle the
# integration applies when the config entry carries no explicit value.
DEFAULT_MIN_PRICE_SPREAD = 0.05

# Boundary-power fixed point: iterate until the efficiency stops moving.
_BOUNDARY_SOLVER_MAX_ITER = 12
_BOUNDARY_SOLVER_TOL = 1e-9


def _solve_drain_w(
    delta_wh: float, step_hours: float, curve: EfficiencyCurve, seed_eff: float
) -> float:
    """AC discharge power that draws exactly delta_wh from the battery."""
    eff = seed_eff
    for _ in range(_BOUNDARY_SOLVER_MAX_ITER):
        next_eff = _interpolate_efficiency(curve, delta_wh * eff / step_hours / 1000.0)
        done = abs(next_eff - eff) < _BOUNDARY_SOLVER_TOL
        eff = next_eff
        if done:
            break
    return delta_wh * eff / step_hours


def _solve_fill_w(
    delta_wh: float, step_hours: float, curve: EfficiencyCurve, seed_eff: float
) -> float:
    """AC charge power that stores exactly delta_wh in the battery."""
    eff = seed_eff
    for _ in range(_BOUNDARY_SOLVER_MAX_ITER):
        next_eff = _interpolate_efficiency(
            curve, delta_wh / (step_hours * eff) / 1000.0
        )
        done = abs(next_eff - eff) < _BOUNDARY_SOLVER_TOL
        eff = next_eff
        if done:
            break
    return delta_wh / (step_hours * eff)


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
    charge_efficiency_curve: EfficiencyCurve = None  # type: ignore[assignment]
    discharge_efficiency_curve: EfficiencyCurve = None  # type: ignore[assignment]
    max_grid_power_kw: float = 0.0
    high_soc_charge_threshold_pct: float = 100.0
    high_soc_max_charge_kw: float = 0.0
    low_soc_discharge_threshold_pct: float = 0.0
    low_soc_max_discharge_kw: float = 0.0

    def __post_init__(self) -> None:
        if self.charge_efficiency_curve is None:
            self.charge_efficiency_curve = _flat_curve(
                self.charge_efficiency, self.max_charge_power_kw
            )
        if self.discharge_efficiency_curve is None:
            self.discharge_efficiency_curve = _flat_curve(
                self.discharge_efficiency, self.max_discharge_power_kw
            )

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
    battery: "BatteryConfig", charge_eff_correction: float | None
) -> "EfficiencyCurve | None":
    """Return corrected charge efficiency curve when diagnostics contain a calibration."""
    if charge_eff_correction is None or charge_eff_correction >= 0.995:
        return None
    return [
        (p, min(1.0, eff * charge_eff_correction))
        for p, eff in battery.charge_efficiency_curve
    ]


def get_discharge_eff_override(
    battery: "BatteryConfig", discharge_eff_correction: float | None
) -> "EfficiencyCurve | None":
    """Return corrected discharge efficiency curve when diagnostics contain a calibration."""
    if discharge_eff_correction is None or discharge_eff_correction >= 0.995:
        return None
    # The correction is an efficiency factor in both directions, so the curve is
    # scaled the same way as the charge side, and bounded at 1.0 for the same
    # reason: a point above 1.0 describes a battery returning more than it was
    # given, and the DP plans discharges it cannot deliver.
    return [
        (p, min(1.0, max(1e-6, eff * discharge_eff_correction)))
        for p, eff in battery.discharge_efficiency_curve
    ]


def extract_inputs(diag: dict) -> tuple:
    """Extract all optimizer inputs from diagnostics.json."""
    bc = diag["data"]["battery_config"]
    charge_eff_scalar = bc.get(
        "charge_efficiency", math.sqrt(bc.get("round_trip_efficiency", 0.90))
    )
    discharge_eff_scalar = bc.get(
        "discharge_efficiency", math.sqrt(bc.get("round_trip_efficiency", 0.90))
    )
    max_chg_kw = bc.get("max_charge_power_kw", 5.0)
    max_dis_kw = bc.get("max_discharge_power_kw", 5.0)

    # Build efficiency curves: use stored curve data if present, else flat scalar curve
    raw_chg_curve = bc.get("charge_efficiency_curve_parsed")
    if raw_chg_curve and isinstance(raw_chg_curve, list):
        charge_curve: EfficiencyCurve = [tuple(p) for p in raw_chg_curve]  # type: ignore[misc]
    else:
        charge_curve = _flat_curve(charge_eff_scalar, max_chg_kw)

    raw_dis_curve = bc.get("discharge_efficiency_curve_parsed")
    if raw_dis_curve and isinstance(raw_dis_curve, list):
        discharge_curve: EfficiencyCurve = [tuple(p) for p in raw_dis_curve]  # type: ignore[misc]
    else:
        discharge_curve = _flat_curve(discharge_eff_scalar, max_dis_kw)

    battery = BatteryConfig(
        capacity_kwh=bc["capacity_kwh"],
        usable_capacity_kwh=bc["usable_capacity_kwh"],
        max_charge_power_kw=max_chg_kw,
        max_discharge_power_kw=max_dis_kw,
        round_trip_efficiency=bc.get(
            "round_trip_efficiency", charge_eff_scalar * discharge_eff_scalar
        ),
        charge_efficiency=charge_eff_scalar,
        discharge_efficiency=discharge_eff_scalar,
        min_soc_percent=bc["min_soc_percent"],
        max_soc_percent=bc["max_soc_percent"],
        min_soc_kwh=bc["min_soc_kwh"],
        max_soc_kwh=bc["max_soc_kwh"],
        pv_dc_coupled=bc["pv_dc_coupled"],
        pv_dc_peak_power_kwp=bc["pv_dc_peak_power_kwp"],
        pv_dc_efficiency=bc["pv_dc_efficiency"],
        charge_efficiency_curve=charge_curve,
        discharge_efficiency_curve=discharge_curve,
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
    # The key is absent from the config entry until the user changes it, so the
    # fallback here has to be the integration's own default (DEFAULT_MIN_PRICE_SPREAD
    # in const.py). Falling back to 0 reported a zero arbitrage hurdle — and with it
    # wrong break-even prices — for every untouched configuration, which is most of
    # them.
    min_price_spread = options.get("min_price_spread", DEFAULT_MIN_PRICE_SPREAD)
    fixed_feed_in_price = options.get("fixed_feed_in_price", 0.04)

    # Use actual feed-in forecast from schedule if available (new diagnostics field)
    feed_in_forecast = sched.get("feed_in_price_forecast") or list(price_forecast)

    # Shadow price from the diagnostics snapshot; informational only (not a DP input).
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
MAX_SOC_STATES = 1000
MIN_CYCLE_KWH = 0.2
POWER_IDLE_THRESHOLD_KW = 0.001
MIN_PV_SURPLUS_KW = 0.05
DC_TO_AC_INVERTER_EFFICIENCY = 0.96


def grid_cap_charge_limit_w(
    max_grid_power_kw: float, consumption_w: float, ac_pv_w: float
) -> float:
    """Largest AC charge setpoint that keeps grid import within the connection cap.

    Mirrors optimizer.grid_cap_charge_limit_w: the cap bounds the action, not
    the price of the flow. Clipping import cost instead made every watt above
    the cap free, so the DP charged at full power whenever the cap was binding.
    """
    if max_grid_power_kw <= 0:
        return float("inf")
    return max(0.0, max_grid_power_kw * 1000.0 - consumption_w + ac_pv_w)


def charge_budget_wh(
    max_charge_power_kw: float, time_step_hours: float, ac_stored_wh: float
) -> float:
    """Battery-side charge energy still available in this step, in Wh."""
    return max(0.0, max_charge_power_kw * 1000.0 * time_step_hours - ac_stored_wh)


def passive_dc_charge_wh(
    pv_dc_production_w: float,
    dc_efficiency: float,
    time_step_hours: float,
    headroom_wh: float,
    budget_wh: float,
) -> float:
    """Energy the DC MPPT absorbs into the battery over one step, in Wh.

    Bounded by production, SoC headroom AND the battery's remaining charge-power
    budget — the last one stops a DC array larger than the inverter charging the
    pack faster than it physically can.
    """
    return max(
        0.0,
        min(
            pv_dc_production_w * dc_efficiency * time_step_hours,
            headroom_wh,
            budget_wh,
        ),
    )


def calculate_step_cost(
    time_step_hours,
    soc_wh,
    action_w,
    grid_price,
    feed_in_price,
    pv_production_w,
    consumption_w,
    charge_curve,
    discharge_curve,
    degradation_cost_per_kwh,
    battery_config,
    pv_dc_production_w=0.0,
    charge_eff=None,
    discharge_eff=None,
    arbitrage_cost_per_kwh=0.0,
):
    # arbitrage_cost_per_kwh is half the configured min_price_spread, charged
    # per kWh of COMMANDED AC throughput so one full cycle carries
    # 2 x degradation + min_price_spread.  Passive DC PV is exempt: it happens
    # whatever the setpoint is, so it is not an arbitrage decision.
    # charge_eff / discharge_eff may be supplied pre-interpolated (hot-path
    # optimisation); they MUST come from the same curves passed above.
    # Interpolate only when the caller did not pre-compute (mirrors optimizer.py).
    if charge_eff is None or discharge_eff is None:
        action_kw = abs(action_w) / 1000.0
        if charge_eff is None:
            charge_eff = _interpolate_efficiency(charge_curve, action_kw)
        if discharge_eff is None:
            discharge_eff = _interpolate_efficiency(discharge_curve, action_kw)
    # Only read on the DC-coupled paths below; the MPPT path never goes through
    # the AC charger, so it does not use the charge curve.
    dc_eff = battery_config.pv_dc_efficiency

    pv_production_w = max(0.0, pv_production_w)
    pv_dc_production_w = max(0.0, pv_dc_production_w)

    dc_pv_excess_w = pv_dc_production_w
    ac_throughput_kwh = 0.0
    passive_throughput_kwh = 0.0

    if action_w > 0:
        # AC charging: grid draws the AC setpoint; passive DC MPPT charging
        # continues on top, limited by the remaining headroom.
        grid_to_battery_w = action_w  # grid draws AC setpoint power
        ac_stored_wh = action_w * time_step_hours * charge_eff
        ac_throughput_kwh = ac_stored_wh / 1000  # actual stored Wh via AC
        if battery_config.pv_dc_coupled and pv_dc_production_w > 0:
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
    elif action_w < 0:
        dc_pv_excess_w = pv_dc_production_w
        usable_power_w = abs(action_w)  # AC output = discharge setpoint
        grid_to_battery_w = -usable_power_w
        ac_throughput_kwh = (
            abs(action_w) * time_step_hours / discharge_eff / 1000
        )  # actual battery-drawn Wh
    else:
        grid_to_battery_w = 0.0
        if battery_config.pv_dc_coupled and pv_dc_production_w > 0:
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

    dc_pv_to_ac_w = (
        dc_pv_excess_w * DC_TO_AC_INVERTER_EFFICIENCY if dc_pv_excess_w > 0 else 0.0
    )
    total_ac_pv_w = pv_production_w + dc_pv_to_ac_w
    net_grid_w = consumption_w - total_ac_pv_w + grid_to_battery_w

    # Export side only: curtailed PV earns nothing, which is the right model.
    # Import is bounded on the action set instead (grid_cap_charge_limit_w), so
    # that over-cap import does not become free.
    if battery_config.max_grid_power_kw > 0:
        net_grid_w = max(-battery_config.max_grid_power_kw * 1000, net_grid_w)

    energy_kwh = abs(net_grid_w) * time_step_hours / 1000
    if net_grid_w > 0:
        grid_cost = energy_kwh * grid_price
    else:
        grid_cost = -energy_kwh * feed_in_price

    degradation_cost = (
        ac_throughput_kwh + passive_throughput_kwh
    ) * degradation_cost_per_kwh
    arbitrage_cost = ac_throughput_kwh * arbitrage_cost_per_kwh
    return grid_cost + degradation_cost + arbitrage_cost


def _find_nearest_soc_idx(soc_wh, soc_states):
    if len(soc_states) <= 1:
        return 0
    step = soc_states[1] - soc_states[0]
    idx = round((soc_wh - soc_states[0]) / step)
    return max(0, min(idx, len(soc_states) - 1))


def _n_states_below_min(current_soc_wh, min_soc_wh, soc_resolution_wh):
    """Number of DP states below min_soc (mirrors optimizer.n_states_below_min)."""
    if current_soc_wh >= min_soc_wh or soc_resolution_wh <= 0:
        return 0
    n_below = math.ceil((min_soc_wh - current_soc_wh) / soc_resolution_wh - 1e-9)
    return min(n_below, MAX_SOC_STATES)


def _deficit_penalty_per_kwh(
    price_forecast,
    feed_in_forecast,
    charge_curve,
    degradation_cost_per_kwh,
    arbitrage_cost_per_kwh,
):
    """Terminal cost per kWh below min_soc (mirrors optimizer.py)."""
    max_energy_price = max([0.0, *price_forecast, *feed_in_forecast])
    min_eff = min((eff for _, eff in charge_curve), default=1.0)
    min_eff = max(0.05, min(1.0, min_eff))
    worst_cost = (
        max_energy_price + max(0.0, degradation_cost_per_kwh) + arbitrage_cost_per_kwh
    ) / min_eff
    return 2.0 * worst_cost + 0.10


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
    charge_eff_curve_override=None,
    discharge_eff_curve_override=None,
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

    min_soc_wh = round(battery_config.min_soc_kwh * 1000)
    max_soc_wh = round(battery_config.max_soc_kwh * 1000)

    # Coarsen the SoC grid for large batteries so the state count stays
    # bounded (mirrors optimizer.py).
    # The below-min extension (see _n_states_below_min) shares the budget.
    soc_resolution_wh = max(
        float(SOC_RESOLUTION_WH),
        (max_soc_wh - min_soc_wh) / MAX_SOC_STATES,
        max(0.0, min_soc_wh - current_soc_kwh * 1000.0) / MAX_SOC_STATES,
    )
    full_step_hours = (
        step_durations_hours[1] if len(step_durations_hours) > 1 else min_step_hours
    )
    aligned_step_w = soc_resolution_wh / full_step_hours
    power_step_w = max(float(POWER_STEP_W), aligned_step_w)

    # Overrides apply to SoC TRANSITIONS only; the cost model always uses the
    # nominal curves (mirrors optimizer.py).
    charge_curve = (
        charge_eff_curve_override
        if charge_eff_curve_override is not None
        else battery_config.charge_efficiency_curve
    )
    discharge_curve = (
        discharge_eff_curve_override
        if discharge_eff_curve_override is not None
        else battery_config.discharge_efficiency_curve
    )
    cost_charge_curve = battery_config.charge_efficiency_curve
    cost_discharge_curve = battery_config.discharge_efficiency_curve

    # Arbitrage hurdle: half the configured spread per direction, so a full
    # cycle carries 2 x degradation + min_price_spread (mirrors optimizer.py).
    arbitrage_cost_per_kwh = max(0.0, min_price_spread) / 2.0
    # Scalar at zero power for boundary action estimation
    _chg_eff_seed = _representative_efficiency(
        charge_curve, battery_config.max_charge_power_kw
    )
    _dis_eff_seed = _representative_efficiency(
        discharge_curve, battery_config.max_discharge_power_kw
    )

    n_soc_states = int(round((max_soc_wh - min_soc_wh) / soc_resolution_wh)) + 1
    if n_soc_states > 1:
        # Exact-fit grid so the top state IS max_soc_wh (mirrors optimizer.py).
        soc_resolution_wh = (max_soc_wh - min_soc_wh) / (n_soc_states - 1)
    # Extend the grid below min_soc when the current SoC lies there, so a
    # below-min start can be charged back up (mirrors optimizer.py, #215).
    min_soc_idx = _n_states_below_min(
        current_soc_kwh * 1000.0, min_soc_wh, soc_resolution_wh
    )
    grid_floor_wh = min_soc_wh - min_soc_idx * soc_resolution_wh
    n_soc_states += min_soc_idx
    soc_states = [grid_floor_wh + i * soc_resolution_wh for i in range(n_soc_states)]

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
    # Ending below min_soc is penalised above any refill cost (mirrors
    # optimizer.py deficit_penalty_per_kwh).
    deficit_penalty = _deficit_penalty_per_kwh(
        price_forecast[:n_steps],
        feed_in_forecast[:n_steps],
        charge_curve,
        degradation_cost_per_kwh,
        arbitrage_cost_per_kwh,
    )
    for s_idx, soc_wh in enumerate(soc_states):
        stored_kwh = (soc_wh - min_soc_wh) / 1000.0
        if stored_kwh >= 0:
            V[n_steps][s_idx] = -stored_kwh * terminal_price
        else:
            V[n_steps][s_idx] = -stored_kwh * deficit_penalty

    max_charge_w = battery_config.max_charge_power_kw * 1000
    max_discharge_w = battery_config.max_discharge_power_kw * 1000
    charge_steps = int(max_charge_w / power_step_w)
    charge_actions = [float(i * power_step_w) for i in range(charge_steps, -1, -1)]
    discharge_steps = int(max_discharge_w / power_step_w)
    discharge_actions = [
        float(-i * power_step_w) for i in range(discharge_steps, 0, -1)
    ]
    actions = discharge_actions + charge_actions

    # Pre-compute per-action efficiencies once (mirrors optimizer.py):
    # transition effs from the (possibly overridden) curves, cost effs from
    # the nominal curves.
    trans_charge_eff = {
        a: _interpolate_efficiency(charge_curve, abs(a) / 1000.0) for a in actions
    }
    trans_discharge_eff = {
        a: _interpolate_efficiency(discharge_curve, abs(a) / 1000.0) for a in actions
    }
    cost_charge_eff = {
        a: _interpolate_efficiency(cost_charge_curve, abs(a) / 1000.0) for a in actions
    }
    cost_discharge_eff = {
        a: _interpolate_efficiency(cost_discharge_curve, abs(a) / 1000.0)
        for a in actions
    }

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

        # Charging is bounded by the battery (SoC derating) and by the grid
        # connection; whichever binds first wins.
        step_charge_cap_w = grid_cap_charge_limit_w(
            battery_config.max_grid_power_kw, consumption_w, pv_w
        )

        for s_idx, soc_wh in enumerate(soc_states):
            best_cost = INF
            best_action = 0.0
            max_chg_w = min(soc_max_charge_w[s_idx], step_charge_cap_w)
            max_dis_w = soc_max_discharge_w[s_idx]

            for action_w in actions:
                if action_w > 0:
                    if action_w > max_chg_w:
                        continue
                    _chg_eff = trans_charge_eff[action_w]
                    energy_change_wh = action_w * time_step_hours * _chg_eff
                    new_soc_wh = soc_wh + energy_change_wh
                    if new_soc_wh > max_soc_wh:
                        continue
                    if battery_config.pv_dc_coupled and pv_dc_w > 0:
                        headroom_wh = max(0.0, max_soc_wh - new_soc_wh)
                        new_soc_wh += passive_dc_charge_wh(
                            pv_dc_w,
                            battery_config.pv_dc_efficiency,
                            time_step_hours,
                            headroom_wh,
                            charge_budget_wh(
                                battery_config.max_charge_power_kw,
                                time_step_hours,
                                energy_change_wh,
                            ),
                        )
                elif action_w < 0:
                    if -action_w > max_dis_w:
                        continue
                    _dis_eff = trans_discharge_eff[action_w]
                    energy_change_wh = abs(action_w) * time_step_hours / _dis_eff
                    new_soc_wh = soc_wh - energy_change_wh
                    if new_soc_wh < min_soc_wh:
                        continue
                else:
                    if battery_config.pv_dc_coupled and pv_dc_w > 0:
                        headroom_wh = max(0.0, max_soc_wh - soc_wh)
                        new_soc_wh = soc_wh + passive_dc_charge_wh(
                            pv_dc_w,
                            battery_config.pv_dc_efficiency,
                            time_step_hours,
                            headroom_wh,
                            charge_budget_wh(
                                battery_config.max_charge_power_kw,
                                time_step_hours,
                                0.0,
                            ),
                        )
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
                    charge_curve=cost_charge_curve,
                    discharge_curve=cost_discharge_curve,
                    degradation_cost_per_kwh=degradation_cost_per_kwh,
                    battery_config=battery_config,
                    pv_dc_production_w=pv_dc_w,
                    arbitrage_cost_per_kwh=arbitrage_cost_per_kwh,
                    charge_eff=cost_charge_eff[action_w],
                    discharge_eff=cost_discharge_eff[action_w],
                )

                total_cost = step_cost + V[t + 1][new_soc_idx]
                if total_cost < best_cost:
                    best_cost = total_cost
                    best_action = action_w

            if soc_wh > min_soc_wh:
                drain_w = _solve_drain_w(
                    soc_wh - min_soc_wh, time_step_hours, discharge_curve, _dis_eff_seed
                )
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
                    total_cost = step_cost + V[t + 1][min_soc_idx]
                    if total_cost < best_cost:
                        best_cost = total_cost
                        best_action = -drain_w
            if soc_wh < max_soc_wh:
                fill_w = _solve_fill_w(
                    max_soc_wh - soc_wh, time_step_hours, charge_curve, _chg_eff_seed
                )
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
    charge_eff_curve_override=None,
    discharge_eff_curve_override=None,
    *,
    arbitrage_cost_per_kwh,
):
    """Execute the forward pass via V-table re-evaluation at the actual continuous SoC.

    arbitrage_cost_per_kwh has no default on purpose: the backward pass applies
    the hurdle, so a forward pass that silently omitted it would re-evaluate the
    V table under a different objective and pick different actions.
    """
    # Overrides apply to SoC TRANSITIONS only; costs use the nominal curves
    # (mirrors optimizer.py).
    charge_curve = (
        charge_eff_curve_override
        if charge_eff_curve_override is not None
        else battery_config.charge_efficiency_curve
    )
    discharge_curve = (
        discharge_eff_curve_override
        if discharge_eff_curve_override is not None
        else battery_config.discharge_efficiency_curve
    )
    cost_charge_curve = battery_config.charge_efficiency_curve
    cost_discharge_curve = battery_config.discharge_efficiency_curve
    _chg_eff_seed = _representative_efficiency(
        charge_curve, battery_config.max_charge_power_kw
    )
    _dis_eff_seed = _representative_efficiency(
        discharge_curve, battery_config.max_discharge_power_kw
    )
    n_soc_states = len(soc_states)
    # The grid may extend below min_soc (see run_dp); min_soc is a grid point.
    min_soc_idx = _find_nearest_soc_idx(min_soc_wh, soc_states)

    max_charge_w = battery_config.max_charge_power_kw * 1000
    max_discharge_w = battery_config.max_discharge_power_kw * 1000
    charge_steps = int(max_charge_w / power_step_w)
    discharge_steps = int(max_discharge_w / power_step_w)
    charge_actions = [float(i * power_step_w) for i in range(charge_steps, -1, -1)]
    discharge_actions = [
        float(-i * power_step_w) for i in range(discharge_steps, 0, -1)
    ]
    actions = discharge_actions + charge_actions

    # Pre-compute per-action efficiencies once (mirrors optimizer.py).
    trans_charge_eff = {
        a: _interpolate_efficiency(charge_curve, abs(a) / 1000.0) for a in actions
    }
    trans_discharge_eff = {
        a: _interpolate_efficiency(discharge_curve, abs(a) / 1000.0) for a in actions
    }
    cost_charge_eff = {
        a: _interpolate_efficiency(cost_charge_curve, abs(a) / 1000.0) for a in actions
    }
    cost_discharge_eff = {
        a: _interpolate_efficiency(cost_discharge_curve, abs(a) / 1000.0)
        for a in actions
    }

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

        for action_w in actions:
            if action_w > 0:
                if action_w > max_chg_w:
                    continue
                _chg_eff = trans_charge_eff[action_w]
                new_soc_wh = current_soc + action_w * time_step_hours * _chg_eff
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
                            action_w * time_step_hours * _chg_eff,
                        ),
                    )
            elif action_w < 0:
                if -action_w > max_dis_w:
                    continue
                _dis_eff = trans_discharge_eff[action_w]
                new_soc_wh = current_soc - abs(action_w) * time_step_hours / _dis_eff
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
                arbitrage_cost_per_kwh=arbitrage_cost_per_kwh,
                charge_eff=cost_charge_eff[action_w],
                discharge_eff=cost_discharge_eff[action_w],
            )
            total_cost = step_cost + V[t + 1][new_soc_idx]
            if total_cost < best_cost:
                best_cost = total_cost
                best_action = action_w
                best_new_soc = new_soc_wh

        # Boundary actions: exact power to drain to min or fill to max SoC
        if current_soc > min_soc_wh:
            drain_w = _solve_drain_w(
                current_soc - min_soc_wh,
                time_step_hours,
                discharge_curve,
                _dis_eff_seed,
            )
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
                total_cost = step_cost + V[t + 1][min_soc_idx]
                if total_cost < best_cost:
                    best_cost = total_cost
                    best_action = -drain_w
                    best_new_soc = float(min_soc_wh)

        if current_soc < max_soc_wh:
            fill_w = _solve_fill_w(
                max_soc_wh - current_soc, time_step_hours, charge_curve, _chg_eff_seed
            )
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
            mode_schedule.append("charging")
        elif best_action < 0:
            mode_schedule.append("discharging")
        else:
            mode_schedule.append("idle")
        current_soc = best_new_soc
        soc_schedule_kwh.append(current_soc / 1000)

    return power_schedule_kw, mode_schedule, soc_schedule_kwh


# ---------------------------------------------------------------------------
# Post-DP filters (verbatim copies of optimizer.py, no HA imports)
#
# Generated from optimizer.py with only these substitutions: the *_parsed
# curve attributes, the efficiency helpers' leading underscore and the
# ACTION_* string constants. Keep them identical when optimizer.py changes;
# tests/test_cross_impl.py checks the outcome against the integration.
# ---------------------------------------------------------------------------


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
            _charge_eff = _interpolate_efficiency(charge_curve, abs(commanded_power_kw))
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
            _discharge_eff = _interpolate_efficiency(
                discharge_curve, abs(commanded_power_kw)
            )
            # Floor at min_soc, but never above where the SoC already is: a
            # discharge left in place while the SoC is below min_soc (after a
            # filter removed the refill before it) must become idle, not a
            # jump UP to min_soc.
            current_soc_kwh = max(
                current_soc_kwh - abs(commanded_power_kw) * step_h / _discharge_eff,
                min(min_soc_kwh, current_soc_kwh),
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
            rebuilt_mode.append("charging")
        elif actual_power_kw < -POWER_IDLE_THRESHOLD_KW:
            rebuilt_mode.append("discharging")
        else:
            rebuilt_mode.append("idle")
        soc_schedule.append(current_soc_kwh)

    return rebuilt_power, rebuilt_mode, soc_schedule


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
            charge_curve=battery_config.charge_efficiency_curve,
            discharge_curve=battery_config.discharge_efficiency_curve,
            degradation_cost_per_kwh=degradation_cost_per_kwh,
            battery_config=battery_config,
            pv_dc_production_w=(pv_dc_forecast[t] if t < len(pv_dc_forecast) else 0.0)
            * 1000,
        )

    final_soc_kwh = soc_schedule_kwh[-1] if soc_schedule_kwh else 0.0
    # Linear in both directions: a schedule that starts below min_soc and ends
    # there too is valued the same way as the initial SoC (see savings), so an
    # idle schedule still reports zero savings and a refill is credited at the
    # terminal price rather than counted as a pure loss.
    # Same Wh-rounded min_soc as the DP grid, so a schedule that drains exactly
    # to the DP's min_soc is not valued as a tiny deficit.
    final_stored_kwh = final_soc_kwh - round(battery_config.min_soc_kwh * 1000) / 1000
    total_cost -= final_stored_kwh * terminal_price
    return total_cost


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
    _rte = _representative_efficiency(
        cost_charge_curve, max_charge_power_kw
    ) * _representative_efficiency(cost_discharge_curve, max_discharge_power_kw)
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
            if filtered_mode[i] == "charging":
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
                    if filtered_mode[j] == "discharging":
                        has_discharge_in_window = True
                        discharge_price = get_discharge_value(j, abs(filtered_power[j]))
                        effective_spread = discharge_price - charge_cost / _rte
                        if effective_spread >= min_arbitrage_spread:
                            has_profitable_discharge = True
                            break
                if has_discharge_in_window and not has_profitable_discharge:
                    filtered_power[i] = 0.0
                    filtered_mode[i] = "idle"
                    changed = True
            elif filtered_mode[i] == "discharging":
                # Look ahead for a charge within the oscillation window.
                # Same logic: suppress only if every charge in the window
                # would make this discharge unprofitable.
                discharge_price = get_discharge_value(i, abs(filtered_power[i]))
                has_charge_in_window = False
                has_profitable_charge = False
                for j in range(i + 1, min(i + lookahead_steps + 1, len(filtered_mode))):
                    if filtered_mode[j] == "charging":
                        has_charge_in_window = True
                        charge_cost = get_charge_cost(j, max(filtered_power[j], 0.0))
                        effective_spread = discharge_price - charge_cost / _rte
                        if effective_spread >= min_arbitrage_spread:
                            has_profitable_charge = True
                            break
                if has_charge_in_window and not has_profitable_charge:
                    filtered_power[i] = 0.0
                    filtered_mode[i] = "idle"
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
    soc_schedule_kwh: list[float] | None = None,
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
        soc_schedule_kwh: SoC trajectory of the input schedule. When given, a
            charge block that starts below min_soc is never suppressed: it is
            the refill back to the reserve, not avoidable wear, and dropping
            it would leave the battery below min_soc run after run.

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
        if current_dir not in ("charging", "discharging"):
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
            if current_dir == "charging":
                total_energy_kwh += (
                    power_kw * step_h * _interpolate_efficiency(charge_curve, power_kw)
                )
            else:
                eff = _interpolate_efficiency(discharge_curve, power_kw)
                total_energy_kwh += power_kw * step_h / eff if eff > 0 else 0.0
            j += 1

        is_refill = (
            current_dir == "charging"
            and soc_schedule_kwh is not None
            and i < len(soc_schedule_kwh)
            and soc_schedule_kwh[i] < min_soc_kwh - 1e-9
        )
        if total_energy_kwh < min_cycle_kwh and not is_refill:
            for k in range(i, j):
                filtered_power[k] = 0.0
                filtered_mode[k] = "idle"
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


def post_process(
    battery,
    current_soc_kwh,
    power_schedule_kw,
    mode_schedule,
    soc_schedule_kwh,
    price_forecast,
    feed_in_forecast,
    pv_forecast,
    consumption_forecast,
    step_durations_hours,
    degradation_cost,
    min_price_spread,
    terminal_price,
    soc_resolution_wh,
    pv_dc_forecast=None,
    charge_eff_curve_override=None,
    discharge_eff_curve_override=None,
):
    """Apply the integration's post-DP pipeline to a forward-pass schedule.

    Mirrors optimize_battery_schedule after the forward pass: price the raw
    schedule, run the oscillation filter and keep it only when it does not cost
    money, then the (ungated) micro-cycle filter.

    Returns (power_kw, modes, soc_kwh, raw_total_cost, total_cost).
    """
    n_steps = len(power_schedule_kw)
    charge_curve = charge_eff_curve_override or battery.charge_efficiency_curve
    discharge_curve = discharge_eff_curve_override or battery.discharge_efficiency_curve
    cost_charge_curve = battery.charge_efficiency_curve
    cost_discharge_curve = battery.discharge_efficiency_curve
    feed_in = (
        feed_in_forecast[:n_steps] if feed_in_forecast else price_forecast[:n_steps]
    )
    pv_dc = pv_dc_forecast[:n_steps] if pv_dc_forecast else None

    usable_kwh = battery.max_soc_kwh - battery.min_soc_kwh
    cycle_hours = (
        usable_kwh / battery.max_discharge_power_kw
        if battery.max_discharge_power_kw > 0
        else 2.0
    )
    oscillation_window_hours = max(2.0, cycle_hours)

    def _price(power_kw, soc_kwh):
        return _calculate_schedule_total_cost(
            battery_config=battery,
            power_schedule_kw=power_kw,
            soc_schedule_kwh=soc_kwh,
            price_forecast=price_forecast[:n_steps],
            feed_in_forecast=feed_in,
            pv_forecast=pv_forecast[:n_steps],
            consumption_forecast=consumption_forecast[:n_steps],
            step_durations_hours=step_durations_hours[:n_steps],
            degradation_cost_per_kwh=degradation_cost,
            terminal_price=terminal_price,
            pv_dc_forecast=pv_dc if pv_dc is not None else [0.0] * n_steps,
        )

    raw_total = _price(power_schedule_kw, soc_schedule_kwh)

    # Ending below min_soc carries the DP's deficit penalty in the cost guard,
    # so the filter cannot undo a refill the DP planned on purpose (mirrors
    # _deficit_surcharge in optimizer.py, #215).
    min_soc_wh = round(battery.min_soc_kwh * 1000)
    deficit_penalty = _deficit_penalty_per_kwh(
        price_forecast[:n_steps],
        feed_in,
        charge_curve,
        degradation_cost,
        max(0.0, min_price_spread) / 2.0,
    )

    def _deficit_surcharge(soc_kwh):
        deficit_kwh = min_soc_wh / 1000.0 - (soc_kwh[-1] if soc_kwh else 0.0)
        if deficit_kwh * 1000.0 <= soc_resolution_wh / 2.0:
            return 0.0
        return deficit_kwh * (deficit_penalty - terminal_price)

    raw_objective = raw_total + _deficit_surcharge(soc_schedule_kwh)

    osc_power, osc_modes, osc_soc = _filter_oscillations(
        power_schedule_kw=power_schedule_kw,
        mode_schedule=mode_schedule,
        initial_soc_kwh=current_soc_kwh,
        price_forecast=price_forecast[:n_steps],
        min_price_spread=min_price_spread,
        degradation_cost_per_kwh=degradation_cost,
        step_durations_hours=step_durations_hours[:n_steps],
        min_soc_kwh=battery.min_soc_kwh,
        max_soc_kwh=battery.max_soc_kwh,
        pv_forecast=pv_forecast[:n_steps],
        consumption_forecast=consumption_forecast[:n_steps],
        feed_in_forecast=feed_in,
        oscillation_window_hours=oscillation_window_hours,
        pv_dc_forecast=pv_dc,
        pv_dc_coupled=battery.pv_dc_coupled,
        pv_dc_efficiency=battery.pv_dc_efficiency,
        charge_curve=charge_curve,
        discharge_curve=discharge_curve,
        cost_charge_curve=cost_charge_curve,
        cost_discharge_curve=cost_discharge_curve,
        max_charge_power_kw=battery.max_charge_power_kw,
        max_discharge_power_kw=battery.max_discharge_power_kw,
    )
    if _price(osc_power, osc_soc) + _deficit_surcharge(osc_soc) <= raw_objective:
        power_schedule_kw, mode_schedule, soc_schedule_kwh = (
            osc_power,
            osc_modes,
            osc_soc,
        )

    power_schedule_kw, mode_schedule, soc_schedule_kwh = _filter_micro_cycles(
        power_schedule_kw=power_schedule_kw,
        mode_schedule=mode_schedule,
        initial_soc_kwh=soc_schedule_kwh[0],
        step_durations_hours=step_durations_hours[:n_steps],
        min_soc_kwh=battery.min_soc_kwh,
        max_soc_kwh=battery.max_soc_kwh,
        pv_dc_forecast=pv_dc,
        pv_dc_coupled=battery.pv_dc_coupled,
        pv_dc_efficiency=battery.pv_dc_efficiency,
        min_cycle_kwh=MIN_CYCLE_KWH,
        charge_curve=charge_curve,
        discharge_curve=discharge_curve,
        max_charge_power_kw=battery.max_charge_power_kw,
        soc_schedule_kwh=soc_schedule_kwh,
    )
    total = _price(power_schedule_kw, soc_schedule_kwh)
    return power_schedule_kw, mode_schedule, soc_schedule_kwh, raw_total, total


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
    arbitrage_cost_per_kwh=0.0,
):
    """For each step, compute the profitability of charging/discharging.

    arbitrage_cost_per_kwh (= min_price_spread / 2) is part of the DP objective,
    so break-even prices must include it or this report says an action was
    profitable that the optimizer correctly refused.
    """
    sqrt_rte = math.sqrt(battery_config.round_trip_efficiency)
    decision_cost = degradation_cost_per_kwh + arbitrage_cost_per_kwh
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
        charge_profit_vs_terminal = terminal_price - charge_grid_cost - decision_cost

        # Net profit of discharging 1 kWh from battery (vs using terminal value)
        discharge_revenue = price * sqrt_rte  # EUR per kWh discharged
        discharge_profit_vs_terminal = (
            discharge_revenue - terminal_price - decision_cost
        )

        # Break-even discharge price needed to beat terminal value
        breakeven_discharge = (terminal_price + decision_cost) / sqrt_rte

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
    arbitrage_cost=0.0,
):
    """Print a detailed schedule table."""
    sqrt_rte = math.sqrt(battery_config.round_trip_efficiency)
    decision_cost = degradation_cost + arbitrage_cost
    breakeven_discharge = (terminal_price + decision_cost) / sqrt_rte

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
        charge_profit = terminal_price - eff_charge_price / sqrt_rte - decision_cost
        discharge_profit = price * sqrt_rte - terminal_price - decision_cost

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
    arbitrage_cost=0.0,
):
    sqrt_rte = math.sqrt(battery.round_trip_efficiency)
    decision_cost = degradation_cost + arbitrage_cost
    breakeven_discharge = (terminal_price + decision_cost) / sqrt_rte
    breakeven_charge = (
        terminal_price - decision_cost
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
    print(
        f"  Arbitrage hurdle:         {arbitrage_cost:.4f} €/kWh"
        f"  (min_price_spread / 2, charged on commanded throughput)"
    )
    print(
        f"  Decision cost per kWh:    {decision_cost:.4f} €/kWh"
        f"  (degradation + hurdle; drives every break-even below)"
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
        p for p in price_forecast if p * sqrt_rte - terminal_price - decision_cost > 0
    ]
    charge_profitable = [
        p for p in price_forecast if terminal_price - p / sqrt_rte - decision_cost > 0
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
    decision_cost = degradation_cost + max(0.0, min_price_spread) / 2.0

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
        (max_price * sqrt_rte - decision_cost) * 0.99,  # just profitable
    ]:
        tp = round(tp, 4)
        breakeven = (tp + decision_cost) / sqrt_rte
        possible = "YES ✓" if max_price >= breakeven else "NO ✗"
        marker = " ← actual" if abs(tp - terminal_price) < 1e-6 else ""
        print(
            f"  {tp:>15.4f}  {breakeven:>19.4f}  {max_price:>18.4f}  {possible:>19}{marker}"
        )

    print()
    print(
        "  The actual terminal price comes from the feed-in forecast, not the previous"
    )
    print(
        "  run's shadow price: max(0, min(feed_in_forecast[-1], clipped 6h tail average))"
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

    charge_eff_curve_override = get_charge_eff_override(battery, charge_eff_correction)
    discharge_eff_curve_override = get_discharge_eff_override(
        battery, discharge_eff_correction
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

    # The two figures above are the fleet aggregate the DP plans with. Each pack
    # also measures itself, and with more than one battery the average describes
    # neither of them — that breakdown is where a single derated pack shows up.
    for _name, _entry in (opt_top.get("battery_calibration") or {}).items():
        for _direction in ("charge", "discharge"):
            _cal = (_entry or {}).get(_direction) or {}
            _correction = _cal.get("correction")
            if _correction is None:
                continue
            _n = _cal.get("samples", 0)
            _why = _cal.get("last_result", "")
            _state = (
                "never measured"
                if not _n
                else "applied"
                if _cal.get("applied")
                else "stored, not applied"
            )
            print(
                f"    {_name} {_direction}: {_correction:.4f}, n={_n} "
                f"({_state}; last: {_why})"
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
        charge_eff_curve_override=charge_eff_curve_override,
        discharge_eff_curve_override=discharge_eff_curve_override,
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
        charge_eff_curve_override=charge_eff_curve_override,
        discharge_eff_curve_override=discharge_eff_curve_override,
        arbitrage_cost_per_kwh=max(0.0, min_price_spread) / 2.0,
    )

    # Post-DP pipeline (oscillation filter with cost guard, micro-cycle
    # filter), exactly as the integration runs it. The recorded schedule below
    # is the post-processed one, so comparing it against the raw forward pass
    # used to show every filter decision as a mismatch.
    raw_mode_schedule = list(mode_schedule)
    (
        power_schedule_kw,
        mode_schedule,
        soc_schedule_kwh,
        sim_raw_total,
        sim_total,
    ) = post_process(
        battery,
        current_soc_kwh,
        power_schedule_kw,
        mode_schedule,
        soc_schedule_kwh,
        price_forecast,
        feed_in_forecast,
        pv_forecast,
        consumption_forecast,
        step_durations_hours,
        degradation_cost,
        min_price_spread,
        terminal_price,
        soc_resolution_wh,
        pv_dc_forecast=pv_dc_forecast,
        charge_eff_curve_override=charge_eff_curve_override,
        discharge_eff_curve_override=discharge_eff_curve_override,
    )
    n_filtered = sum(1 for a, b in zip(raw_mode_schedule, mode_schedule) if a != b)
    print(
        f"  Post-processing: {n_filtered} step(s) changed by the filters "
        f"(simulated cost {sim_raw_total:.5f} € raw -> {sim_total:.5f} € final)"
    )

    # Recorded schedule from diagnostics
    rec_power = sched.get("power_schedule_kw")
    rec_mode = sched.get("mode_schedule")
    rec_soc = sched.get("soc_schedule_kwh")

    arbitrage_cost = max(0.0, min_price_spread) / 2.0
    print_summary(
        battery,
        current_soc_kwh,
        terminal_price,
        degradation_cost,
        price_forecast,
        feed_in_forecast,
        arbitrage_cost=arbitrage_cost,
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
        arbitrage_cost=arbitrage_cost,
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
            f"{'SoC kWh':>7}  {'Price':>7}  {'Shadow':>7}  {'Surplus':>7}  "
            f"{'Commit':>6}  {'Reason'}"
        )
        print("-" * 132)
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
            if entry.get("grid_charge_vetoed"):
                # The hybrid arbitration refused to buy the planned charge:
                # the shadow price does not cover the buy price, so only PV
                # surplus (if any) is charged.
                dev += " ← grid charge vetoed"
            # PV surplus as the hybrid branches see it (battery - grid), absent
            # in diagnostics from older versions.
            surplus = entry.get("surplus_kw")
            print(
                f"  {ts:>22}  {entry.get('control_mode', ''):>14}  "
                f"{entry.get('dp_mode', ''):>11}  {dp_kw:>6.3f}  "
                f"{entry.get('effective_mode', ''):>11}  {eff_kw:>6.3f}  {setp_kw:>8.3f}  "
                f"{entry.get('soc_kwh', 0):>7.3f}  "
                f"{entry.get('current_price') or 0:>7.4f}  "
                f"{entry.get('shadow_price_eur_kwh') or 0:>7.4f}  "
                f"{'—' if surplus is None else f'{surplus:7.3f}':>7}  "
                f"{locked:>6}  {reason}{dev}"
            )
        print("-" * 132)

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

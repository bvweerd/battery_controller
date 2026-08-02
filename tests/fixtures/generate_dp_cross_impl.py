#!/usr/bin/env python3
"""Generate cross-implementation DP fixtures from optimizer.py (source of truth).

Each case is designed to be post-processing-neutral (no oscillations, blocks
above MIN_CYCLE_KWH) so the filter-less simulator produces the same schedule.
The script validates optimizer == simulator at generation time.
"""

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "simulate"))

from custom_components.battery_controller.battery_model import BatteryConfig  # noqa: E402
from custom_components.battery_controller.optimizer import (  # noqa: E402
    optimize_battery_schedule,
)
import simulate_diagnostics as sim  # noqa: E402


def curve_str(points):
    return ", ".join(f"{p}:{e}" for p, e in points)


CASES = [
    {
        "name": "flat_curve_arbitrage",
        "description": "Flat sqrt(0.90) curves; charge in valley, discharge at peak.",
        "config": {
            "capacity_kwh": 10.0,
            "min_soc_percent": 10.0,
            "max_soc_percent": 90.0,
            "max_charge_power_kw": 2.0,
            "max_discharge_power_kw": 2.0,
            "charge_curve": [[0.0, 0.9487], [2.0, 0.9487]],
            "discharge_curve": [[0.0, 0.9487], [2.0, 0.9487]],
            "pv_dc_coupled": False,
            "pv_dc_efficiency": 0.97,
            "max_grid_power_kw": 0.0,
        },
        "inputs": {
            "soc_kwh": 5.0,
            "prices": [
                0.10,
                0.10,
                0.10,
                0.10,
                0.40,
                0.40,
                0.40,
                0.40,
                0.10,
                0.10,
                0.10,
                0.10,
            ],
            "feed_in": [
                0.08,
                0.08,
                0.08,
                0.08,
                0.35,
                0.35,
                0.35,
                0.35,
                0.08,
                0.08,
                0.08,
                0.08,
            ],
            "pv": [0.0] * 12,
            "consumption": [0.5] * 12,
            "pv_dc": None,
            "step_hours": [1.0] * 12,
            "degradation_cost_per_kwh": 0.01,
            "min_price_spread": 0.05,
        },
    },
    {
        "name": "sloped_curves",
        "description": (
            "Power-dependent curves: high power is less efficient. Battery is "
            "large relative to the discharge window so SoC boundaries stay out "
            "of reach (boundary actions are rebuild-corrected and would differ "
            "from the filter-less simulator)."
        ),
        "config": {
            "capacity_kwh": 20.0,
            "min_soc_percent": 10.0,
            "max_soc_percent": 90.0,
            "max_charge_power_kw": 4.0,
            "max_discharge_power_kw": 4.0,
            "charge_curve": [[0.0, 0.95], [2.0, 0.90], [4.0, 0.80]],
            "discharge_curve": [[0.0, 0.95], [2.0, 0.92], [4.0, 0.75]],
            "pv_dc_coupled": False,
            "pv_dc_efficiency": 0.97,
            "max_grid_power_kw": 0.0,
        },
        "inputs": {
            "soc_kwh": 14.0,
            "prices": [0.50, 0.50, 0.05, 0.05, 0.05, 0.05],
            "feed_in": [0.45, 0.45, 0.02, 0.02, 0.02, 0.02],
            "pv": [0.0] * 6,
            "consumption": [0.3] * 6,
            "pv_dc": None,
            "step_hours": [1.0] * 6,
            "degradation_cost_per_kwh": 0.03,
            "min_price_spread": 0.05,
        },
    },
    {
        "name": "dc_pv_grid_cap",
        "description": (
            "DC-coupled PV absorbed passively under a tight grid cap; low "
            "feed-in keeps the battery idle so the case stays free of "
            "post-processing (exercises the DC-PV and grid-cap cost paths)."
        ),
        "config": {
            "capacity_kwh": 20.0,
            "min_soc_percent": 10.0,
            "max_soc_percent": 90.0,
            "max_charge_power_kw": 3.0,
            "max_discharge_power_kw": 3.0,
            "charge_curve": [[0.0, 0.96], [3.0, 0.88]],
            "discharge_curve": [[0.0, 0.96], [3.0, 0.90]],
            "pv_dc_coupled": True,
            "pv_dc_efficiency": 0.97,
            "max_grid_power_kw": 2.0,
        },
        "inputs": {
            "soc_kwh": 6.0,
            "prices": [0.30] * 8,
            "feed_in": [0.05] * 8,
            "pv": [0.0, 0.5, 1.0, 1.0, 0.5, 0.0, 0.0, 0.0],
            "consumption": [0.4] * 8,
            "pv_dc": [0.0, 1.5, 2.5, 2.5, 1.0, 0.0, 0.0, 0.0],
            "step_hours": [1.0] * 8,
            "degradation_cost_per_kwh": 0.02,
            "min_price_spread": 0.05,
        },
    },
]


def run_integration(case):
    cfg = case["config"]
    inp = case["inputs"]
    bc = BatteryConfig(
        capacity_kwh=cfg["capacity_kwh"],
        max_charge_power_kw=cfg["max_charge_power_kw"],
        max_discharge_power_kw=cfg["max_discharge_power_kw"],
        charge_efficiency_curve=curve_str(cfg["charge_curve"]),
        discharge_efficiency_curve=curve_str(cfg["discharge_curve"]),
        min_soc_percent=cfg["min_soc_percent"],
        max_soc_percent=cfg["max_soc_percent"],
        pv_dc_coupled=cfg["pv_dc_coupled"],
        pv_dc_efficiency=cfg["pv_dc_efficiency"],
        max_grid_power_kw=cfg["max_grid_power_kw"],
    )
    return optimize_battery_schedule(
        battery_config=bc,
        current_soc_kwh=inp["soc_kwh"],
        price_forecast=inp["prices"],
        feed_in_forecast=inp["feed_in"],
        pv_forecast=inp["pv"],
        consumption_forecast=inp["consumption"],
        step_durations_hours=inp["step_hours"],
        degradation_cost_per_kwh=inp["degradation_cost_per_kwh"],
        min_price_spread=inp["min_price_spread"],
        pv_dc_forecast=inp["pv_dc"],
    )


def run_sim(case):
    cfg = case["config"]
    inp = case["inputs"]
    min_soc_kwh = cfg["capacity_kwh"] * cfg["min_soc_percent"] / 100.0
    max_soc_kwh = cfg["capacity_kwh"] * cfg["max_soc_percent"] / 100.0
    bc = sim.BatteryConfig(
        capacity_kwh=cfg["capacity_kwh"],
        usable_capacity_kwh=max_soc_kwh - min_soc_kwh,
        max_charge_power_kw=cfg["max_charge_power_kw"],
        max_discharge_power_kw=cfg["max_discharge_power_kw"],
        round_trip_efficiency=cfg["charge_curve"][0][1] * cfg["discharge_curve"][0][1],
        charge_efficiency=cfg["charge_curve"][0][1],
        discharge_efficiency=cfg["discharge_curve"][0][1],
        min_soc_percent=cfg["min_soc_percent"],
        max_soc_percent=cfg["max_soc_percent"],
        min_soc_kwh=min_soc_kwh,
        max_soc_kwh=max_soc_kwh,
        pv_dc_coupled=cfg["pv_dc_coupled"],
        pv_dc_peak_power_kwp=0.0,
        pv_dc_efficiency=cfg["pv_dc_efficiency"],
        charge_efficiency_curve=[tuple(p) for p in cfg["charge_curve"]],
        discharge_efficiency_curve=[tuple(p) for p in cfg["discharge_curve"]],
        max_grid_power_kw=cfg["max_grid_power_kw"],
    )
    pv_dc = inp["pv_dc"] or [0.0] * len(inp["prices"])
    (
        V,
        policy,
        soc_states,
        soc_res,
        power_step_w,
        step_h,
        min_soc_wh,
        max_soc_wh,
        terminal_price,
    ) = sim.run_dp(
        bc,
        inp["soc_kwh"],
        inp["prices"],
        inp["feed_in"],
        inp["pv"],
        inp["consumption"],
        inp["step_hours"],
        inp["degradation_cost_per_kwh"],
        inp["min_price_spread"],
        pv_dc,
    )
    power, modes, soc = sim.forward_pass(
        V,
        soc_states,
        inp["soc_kwh"],
        step_h,
        min_soc_wh,
        max_soc_wh,
        len(inp["prices"]),
        bc,
        pv_dc,
        inp["prices"],
        inp["feed_in"],
        inp["pv"],
        inp["consumption"],
        inp["degradation_cost_per_kwh"],
        power_step_w,
    )
    return power, soc


out = []
for case in CASES:
    res = run_integration(case)
    sim_power, sim_soc = run_sim(case)
    # Filter-neutrality check: simulator (no post-filters) must match.
    assert len(sim_power) == len(res.power_schedule_kw), case["name"]
    for a, b in zip(res.power_schedule_kw, sim_power):
        assert abs(a - b) < 1e-9, (case["name"], res.power_schedule_kw, sim_power)
    out.append(
        {
            "name": case["name"],
            "description": case["description"],
            "config": case["config"],
            "inputs": case["inputs"],
            "expected": {
                "power_schedule_kw": res.power_schedule_kw,
                "soc_schedule_kwh": res.soc_schedule_kwh,
                "total_cost": res.total_cost,
                "baseline_cost": res.baseline_cost,
            },
        }
    )
    print(f"{case['name']}: schedule={['%.2f' % p for p in res.power_schedule_kw]}")

fixture_path = REPO / "tests" / "fixtures" / "dp_cross_impl.json"
fixture_path.parent.mkdir(exist_ok=True)
with open(fixture_path, "w") as f:
    json.dump(out, f, indent=2)
    f.write("\n")
print(f"wrote {fixture_path}")

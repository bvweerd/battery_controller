"""Cross-implementation DP fixtures: optimizer.py vs simulate_diagnostics.py.

The three DP implementations (optimizer.py, docs/analyzer/analyzer.js,
simulate/simulate_diagnostics.py) must stay algorithmically identical.  The
shared fixtures in tests/fixtures/dp_cross_impl.json pin the integration's
output; this module checks the integration against the fixture (staleness
guard) and the simulator against the same fixture.  docs/__tests__/
cross_impl.test.js runs the identical cases against analyzer.js.

Fixture cases are deliberately post-processing-neutral (no oscillation pairs,
no SoC boundary hits) because the simulator runs the raw DP without the
integration's post-filters.  Regenerate the fixture with the generator noted
inside the JSON file whenever the DP algorithm intentionally changes.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from custom_components.battery_controller.battery_model import BatteryConfig
from custom_components.battery_controller.optimizer import optimize_battery_schedule

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "dp_cross_impl.json"
SIM_PATH = Path(__file__).parent.parent / "simulate" / "simulate_diagnostics.py"

with open(FIXTURE_PATH) as f:
    CASES = json.load(f)


def _load_sim():
    spec = importlib.util.spec_from_file_location("simulate_diagnostics", SIM_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("simulate_diagnostics", module)
    spec.loader.exec_module(module)
    return module


sim = _load_sim()


def _curve_str(points: list[list[float]]) -> str:
    return ", ".join(f"{p}:{e}" for p, e in points)


def _integration_config(cfg: dict) -> BatteryConfig:
    return BatteryConfig(
        capacity_kwh=cfg["capacity_kwh"],
        max_charge_power_kw=cfg["max_charge_power_kw"],
        max_discharge_power_kw=cfg["max_discharge_power_kw"],
        charge_efficiency_curve=_curve_str(cfg["charge_curve"]),
        discharge_efficiency_curve=_curve_str(cfg["discharge_curve"]),
        min_soc_percent=cfg["min_soc_percent"],
        max_soc_percent=cfg["max_soc_percent"],
        pv_dc_coupled=cfg["pv_dc_coupled"],
        pv_dc_efficiency=cfg["pv_dc_efficiency"],
        max_grid_power_kw=cfg["max_grid_power_kw"],
    )


def _sim_config(cfg: dict) -> sim.BatteryConfig:
    min_soc_kwh = cfg["capacity_kwh"] * cfg["min_soc_percent"] / 100.0
    max_soc_kwh = cfg["capacity_kwh"] * cfg["max_soc_percent"] / 100.0
    return sim.BatteryConfig(
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


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_integration_matches_fixture(case: dict) -> None:
    """Guard against fixture staleness: optimizer.py must reproduce it."""
    inp = case["inputs"]
    result = optimize_battery_schedule(
        battery_config=_integration_config(case["config"]),
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
    expected = case["expected"]
    assert result.power_schedule_kw == pytest.approx(
        expected["power_schedule_kw"], abs=1e-9
    )
    assert result.soc_schedule_kwh == pytest.approx(
        expected["soc_schedule_kwh"], abs=1e-9
    )
    assert result.total_cost == pytest.approx(expected["total_cost"], abs=1e-9)
    assert result.baseline_cost == pytest.approx(expected["baseline_cost"], abs=1e-9)


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_simulator_matches_fixture(case: dict) -> None:
    """simulate_diagnostics.py must produce the same schedule as optimizer.py."""
    inp = case["inputs"]
    battery = _sim_config(case["config"])
    pv_dc = inp["pv_dc"] or [0.0] * len(inp["prices"])
    (
        V,
        _policy,
        soc_states,
        _soc_res,
        power_step_w,
        step_durations,
        min_soc_wh,
        max_soc_wh,
        _terminal_price,
    ) = sim.run_dp(
        battery,
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
    power, _modes, soc = sim.forward_pass(
        V,
        soc_states,
        inp["soc_kwh"],
        step_durations,
        min_soc_wh,
        max_soc_wh,
        len(inp["prices"]),
        battery,
        pv_dc,
        inp["prices"],
        inp["feed_in"],
        inp["pv"],
        inp["consumption"],
        inp["degradation_cost_per_kwh"],
        power_step_w,
        arbitrage_cost_per_kwh=max(0.0, inp["min_price_spread"]) / 2.0,
    )
    expected = case["expected"]
    assert power == pytest.approx(expected["power_schedule_kw"], abs=1e-9)
    assert soc == pytest.approx(expected["soc_schedule_kwh"], abs=1e-9)

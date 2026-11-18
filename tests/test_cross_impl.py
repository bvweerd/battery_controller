"""Cross-implementation DP fixtures: optimizer.py vs simulate_diagnostics.py.

The three DP implementations (optimizer.py, docs/analyzer/analyzer.js,
simulate/simulate_diagnostics.py) must stay algorithmically identical.  The
shared fixtures in tests/fixtures/dp_cross_impl.json pin the integration's
output; this module checks the integration against the fixture (staleness
guard) and the simulator against the same fixture.  docs/__tests__/
cross_impl.test.js runs the identical cases against analyzer.js.

Fixture cases are deliberately post-processing-neutral (no oscillation pairs,
no SoC boundary hits) so they pin the raw DP, which analyzer.js is compared on
too.  The post-DP filters are covered separately below by comparing the
simulator's full pipeline (post_process) with optimize_battery_schedule on
scenarios that do trigger them.  Regenerate the fixture with the generator
noted inside the JSON file whenever the DP algorithm intentionally changes.
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


def _scenario(seed: int) -> dict:
    """A quarter-hourly day with noisy prices, PV and load (deterministic)."""
    import random

    rng = random.Random(seed)
    n = 48
    prices = [
        round(0.20 + 0.10 * ((i // 8) % 2) + rng.uniform(-0.03, 0.03), 4)
        for i in range(n)
    ]
    pv = [round(max(0.0, rng.uniform(-1.0, 2.5)), 3) for _ in range(n)]
    load = [round(rng.uniform(0.2, 1.2), 3) for _ in range(n)]
    return {
        "config": {
            "capacity_kwh": 5.0,
            "max_charge_power_kw": 2.0,
            "max_discharge_power_kw": 2.0,
            "charge_curve": [[0.0, 0.9], [2.0, 0.95]],
            "discharge_curve": [[0.0, 0.9], [2.0, 0.95]],
            "min_soc_percent": 10.0,
            "max_soc_percent": 90.0,
            "pv_dc_coupled": False,
            "pv_dc_efficiency": 0.97,
            "max_grid_power_kw": 0.0,
        },
        "inputs": {
            "soc_kwh": 2.0,
            "prices": prices,
            "feed_in": [round(p - 0.12, 4) for p in prices],
            "pv": pv,
            "consumption": load,
            "step_hours": [0.1] + [0.25] * (n - 1),
            "degradation_cost_per_kwh": 0.01,
            "min_price_spread": 0.0,
            "pv_dc": None,
        },
    }


def _simulate_full(case: dict):
    """The simulator's whole pipeline: DP, forward pass, post-DP filters."""
    inp = case["inputs"]
    battery = _sim_config(case["config"])
    n = len(inp["prices"])
    pv_dc = inp["pv_dc"] or [0.0] * n
    (V, _p, soc_states, _r, step_w, durations, min_wh, max_wh, terminal) = sim.run_dp(
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
    power, modes, soc = sim.forward_pass(
        V,
        soc_states,
        inp["soc_kwh"],
        durations,
        min_wh,
        max_wh,
        n,
        battery,
        pv_dc,
        inp["prices"],
        inp["feed_in"],
        inp["pv"],
        inp["consumption"],
        inp["degradation_cost_per_kwh"],
        step_w,
        arbitrage_cost_per_kwh=max(0.0, inp["min_price_spread"]) / 2.0,
    )
    raw_modes = list(modes)
    power, modes, soc, raw_total, total = sim.post_process(
        battery,
        inp["soc_kwh"],
        power,
        modes,
        soc,
        inp["prices"],
        inp["feed_in"],
        inp["pv"],
        inp["consumption"],
        durations,
        inp["degradation_cost_per_kwh"],
        inp["min_price_spread"],
        terminal,
        pv_dc_forecast=pv_dc,
    )
    return power, modes, soc, total, raw_modes != modes


@pytest.mark.parametrize("seed", range(6))
def test_simulator_post_processing_matches_integration(seed: int) -> None:
    """With the filters active, the simulator still reproduces optimizer.py."""
    case = _scenario(seed)
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
    power, modes, soc, total, _changed = _simulate_full(case)

    assert power == pytest.approx(result.power_schedule_kw, abs=1e-9)
    assert modes == result.mode_schedule
    assert soc == pytest.approx(result.soc_schedule_kwh, abs=1e-9)
    assert total == pytest.approx(result.total_cost, abs=1e-9)


def test_post_processing_scenarios_actually_exercise_the_filters() -> None:
    """At least one scenario above must be changed by the filters.

    Otherwise the parity test would pass on a simulator without them.
    """
    assert any(_simulate_full(_scenario(seed))[4] for seed in range(6))

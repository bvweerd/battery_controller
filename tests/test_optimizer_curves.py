"""Behavioural tests for power-dependent efficiency curves in the DP optimizer.

Covers the two properties the curve feature must guarantee:

1. Calibration overrides only affect SoC transitions — the economic cost
   model (grid + degradation) always uses the nominal curves, so a
   charging/discharging-speed problem is never double-counted as cost.
2. With a power-dependent curve the optimizer actually prefers a more
   efficient (lower-power) operating point when the horizon allows it —
   the core promise of the feature.
"""

from __future__ import annotations

import pytest

from custom_components.battery_controller.battery_model import BatteryConfig
from custom_components.battery_controller.efficiency_curve import (
    interpolate_efficiency,
    representative_efficiency,
)
from custom_components.battery_controller.optimizer import (
    calculate_step_cost,
    optimize_battery_schedule,
    solve_boundary_drain_w,
    solve_boundary_fill_w,
)


def test_calibration_override_does_not_change_economic_cost() -> None:
    """Cost model must stay on nominal curves when calibration overrides apply.

    Scenario: single profitable discharge, then idle steps with terminal
    price 0, so the DP cost (raw_total_cost) is exactly the step cost of the
    discharge. A discharge override with eff/0.5 (> 1.0) changes the SoC
    transition but must NOT change the degradation term of the cost.
    """
    config = BatteryConfig(
        capacity_kwh=10.0,
        max_charge_power_kw=1.0,
        max_discharge_power_kw=1.0,
        charge_efficiency_curve="0.9487",
        discharge_efficiency_curve="0.9487",
        min_soc_percent=10.0,
        max_soc_percent=90.0,
    )
    kwargs = dict(
        battery_config=config,
        current_soc_kwh=9.0,
        price_forecast=[0.5, 0.5, 0.5],
        # Median of the feed-in forecast is 0.0 → terminal price 0, so V is
        # independent of the end-of-horizon SoC state.
        feed_in_forecast=[2.0, 0.0, 0.0],
        pv_forecast=[0.0, 0.0, 0.0],
        consumption_forecast=[0.0, 0.0, 0.0],
        step_durations_hours=[1.0, 1.0, 1.0],
        degradation_cost_per_kwh=0.05,
        min_price_spread=0.05,
    )

    nominal = optimize_battery_schedule(**kwargs)

    correction = 0.5
    charge_override = [
        (p, min(1.0, e * correction)) for p, e in config.charge_efficiency_curve_parsed
    ]
    discharge_override = [
        (p, e / correction) for p, e in config.discharge_efficiency_curve_parsed
    ]
    corrected = optimize_battery_schedule(
        **kwargs,
        charge_eff_curve_override=charge_override,
        discharge_eff_curve_override=discharge_override,
    )

    # Same discharge action is chosen in both runs...
    assert nominal.power_schedule_kw[0] == pytest.approx(-1.0)
    assert corrected.power_schedule_kw[0] == pytest.approx(-1.0)
    # ...and its economic cost is identical: the override never enters the
    # cost model (a leak would halve the degradation term here).
    assert corrected.raw_total_cost == pytest.approx(nominal.raw_total_cost, abs=1e-9)
    assert corrected.total_cost == pytest.approx(nominal.total_cost, abs=1e-9)
    # The SoC transition, however, DOES use the override: discharging 1 kWh AC
    # at eff ≈ 1.9 drains ~0.53 kWh instead of ~1.05 kWh.
    assert corrected.soc_schedule_kwh[1] > nominal.soc_schedule_kwh[1]


def test_optimizer_prefers_efficient_power_level() -> None:
    """With a steep curve the DP spreads discharge over more, lower-power steps.

    Limited stored energy, two equally priced peak hours: a flat curve makes
    the split irrelevant (ties break to full power, front-loaded); a steep
    curve makes low-power discharge convert more stored energy into AC
    revenue, so the DP must choose it.
    """
    base = dict(
        capacity_kwh=20.0,
        max_charge_power_kw=4.0,
        max_discharge_power_kw=4.0,
        min_soc_percent=10.0,
        max_soc_percent=90.0,
    )
    inputs = dict(
        # 9.5 kWh above min: more than 2 h × 4 kW can drain, so with a flat
        # curve running BOTH peak hours at full power is the unique optimum.
        current_soc_kwh=11.5,
        price_forecast=[0.5] * 6,
        feed_in_forecast=[2.0, 2.0, 0.0, 0.0, 0.0, 0.0],  # terminal price 0
        pv_forecast=[0.0] * 6,
        consumption_forecast=[0.0] * 6,
        step_durations_hours=[1.0] * 6,
        degradation_cost_per_kwh=0.05,
        min_price_spread=0.05,
    )

    flat_config = BatteryConfig(
        **base,
        charge_efficiency_curve="0.90",
        discharge_efficiency_curve="0.90",
    )
    flat = optimize_battery_schedule(battery_config=flat_config, **inputs)
    # Flat curve: energy exceeds the power limit → both hours at full power
    assert flat.power_schedule_kw[0] == pytest.approx(-4.0)
    assert flat.power_schedule_kw[1] == pytest.approx(-4.0)

    steep_config = BatteryConfig(
        **base,
        charge_efficiency_curve="0.90",
        discharge_efficiency_curve="0:0.95, 2:0.90, 4:0.70",
    )
    steep = optimize_battery_schedule(battery_config=steep_config, **inputs)

    discharge_steps = [p for p in steep.power_schedule_kw[:2] if p < 0]
    # Both peak hours are still used...
    assert len(discharge_steps) == 2
    # ...but the DP backs off from the inefficient full-power region: at 4 kW
    # only 70% of the drawn energy becomes revenue, so a lower power converts
    # the limited stored energy into strictly more AC output.
    assert all(abs(p) < 4.0 for p in discharge_steps)
    total_ac_kwh = sum(-p for p in discharge_steps)
    assert total_ac_kwh > 6.5  # vs 8.0 for flat; naive 4 kW×2 would be infeasible


def test_calculate_step_cost_precomputed_efficiency_is_equivalent() -> None:
    """Passing pre-interpolated efficiencies must not change the result."""
    config = BatteryConfig(
        capacity_kwh=10.0,
        max_charge_power_kw=4.0,
        max_discharge_power_kw=4.0,
        charge_efficiency_curve="0:0.95, 4:0.85",
        discharge_efficiency_curve="0:0.96, 4:0.80",
        min_soc_percent=10.0,
        max_soc_percent=90.0,
    )
    from custom_components.battery_controller.efficiency_curve import (
        interpolate_efficiency,
    )

    for action_w in (-3300.0, -100.0, 0.0, 100.0, 2500.0):
        common = dict(
            time_step_hours=0.25,
            soc_wh=5000.0,
            action_w=action_w,
            grid_price=0.30,
            feed_in_price=0.10,
            pv_production_w=500.0,
            consumption_w=800.0,
            charge_curve=config.charge_efficiency_curve_parsed,
            discharge_curve=config.discharge_efficiency_curve_parsed,
            degradation_cost_per_kwh=0.02,
            battery_config=config,
            pv_dc_production_w=0.0,
        )
        on_the_fly = calculate_step_cost(**common)
        precomputed = calculate_step_cost(
            **common,
            charge_eff=interpolate_efficiency(
                config.charge_efficiency_curve_parsed, abs(action_w) / 1000.0
            ),
            discharge_eff=interpolate_efficiency(
                config.discharge_efficiency_curve_parsed, abs(action_w) / 1000.0
            ),
        )
        assert precomputed == pytest.approx(on_the_fly, abs=0.0)


class TestBoundaryActionFixedPoint:
    """Boundary actions must land on the SoC boundary they claim to reach.

    The boundary power and the efficiency at that power are mutually dependent,
    so they are solved by fixed-point iteration.  These tests check the solved
    power actually satisfies the SoC transition on a steep curve, where a single
    zero-power scalar is badly wrong.
    """

    # Rising part-load curve: efficiency is poor at low power, good at high.
    STEEP = [(0.05, 0.50), (0.2, 0.75), (0.5, 0.88), (1.0, 0.93), (5.0, 0.95)]

    def test_fill_stores_exactly_the_requested_energy(self):
        seed = representative_efficiency(self.STEEP, 5.0)
        for delta_wh in (25.0, 60.0, 115.0, 400.0):
            power_w = solve_boundary_fill_w(delta_wh, 0.25, self.STEEP, seed)
            eff = interpolate_efficiency(self.STEEP, power_w / 1000.0)
            stored_wh = power_w * 0.25 * eff
            assert stored_wh == pytest.approx(delta_wh, rel=1e-3)

    def test_drain_draws_exactly_the_requested_energy(self):
        seed = representative_efficiency(self.STEEP, 5.0)
        for delta_wh in (25.0, 60.0, 115.0, 400.0):
            power_w = solve_boundary_drain_w(delta_wh, 0.25, self.STEEP, seed)
            eff = interpolate_efficiency(self.STEEP, power_w / 1000.0)
            drawn_wh = power_w * 0.25 / eff
            assert drawn_wh == pytest.approx(delta_wh, rel=1e-3)

    def test_zero_power_scalar_would_be_wrong(self):
        """Guard the reason this solver exists."""
        delta_wh = 115.0
        zero_power_eff = interpolate_efficiency(self.STEEP, 0.0)
        naive_fill_w = delta_wh / (0.25 * zero_power_eff)
        naive_stored = (
            naive_fill_w
            * 0.25
            * interpolate_efficiency(self.STEEP, naive_fill_w / 1000.0)
        )
        # The naive estimate overshoots the boundary by more than 30 %
        assert naive_stored > delta_wh * 1.3

        seed = representative_efficiency(self.STEEP, 5.0)
        solved_w = solve_boundary_fill_w(delta_wh, 0.25, self.STEEP, seed)
        solved_stored = (
            solved_w * 0.25 * interpolate_efficiency(self.STEEP, solved_w / 1000.0)
        )
        assert solved_stored == pytest.approx(delta_wh, rel=1e-3)

    def test_flat_curve_matches_closed_form(self):
        """Flat curves must reproduce the pre-curve arithmetic exactly."""
        flat = [(0.0, 0.9487), (5.0, 0.9487)]
        assert solve_boundary_fill_w(100.0, 0.25, flat, 0.9487) == pytest.approx(
            100.0 / (0.25 * 0.9487)
        )
        assert solve_boundary_drain_w(100.0, 0.25, flat, 0.9487) == pytest.approx(
            100.0 * 0.9487 / 0.25
        )

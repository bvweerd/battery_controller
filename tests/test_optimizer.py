"""Tests for optimizer.py."""

import pytest

from custom_components.battery_controller.battery_model import BatteryConfig
from custom_components.battery_controller.optimizer import (
    OptimizationResult,
    calculate_step_cost,
    optimize_battery_schedule,
    should_charge_now,
    should_discharge_now,
    _find_nearest_soc_idx,
)


@pytest.fixture
def battery_config():
    """Standard 10 kWh battery."""
    return BatteryConfig(
        capacity_kwh=10.0,
        max_charge_power_kw=5.0,
        max_discharge_power_kw=5.0,
        round_trip_efficiency=0.90,
        min_soc_percent=10.0,
        max_soc_percent=90.0,
    )


@pytest.fixture
def dc_battery_config():
    """Battery with DC-coupled PV."""
    return BatteryConfig(
        capacity_kwh=10.0,
        max_charge_power_kw=5.0,
        max_discharge_power_kw=5.0,
        round_trip_efficiency=0.90,
        min_soc_percent=10.0,
        max_soc_percent=90.0,
        pv_dc_coupled=True,
        pv_dc_peak_power_kwp=3.0,
        pv_dc_efficiency=0.97,
    )


class TestCalculateStepCost:
    """Tests for calculate_step_cost function."""

    def test_idle_no_pv(self, battery_config):
        """Idle battery, just consumption from grid."""
        cost = calculate_step_cost(
            time_step_hours=0.25,
            soc_wh=5000,
            action_w=0,
            grid_price=0.30,
            feed_in_price=0.07,
            pv_production_w=0,
            consumption_w=1000,
            rte=0.90,
            degradation_cost_per_kwh=0.03,
            battery_config=battery_config,
        )
        # 1000W * 0.25h = 250 Wh = 0.25 kWh * 0.30 = 0.075 EUR
        assert cost == pytest.approx(0.075, abs=0.001)

    def test_idle_with_pv_surplus(self, battery_config):
        """Idle battery, PV surplus exported."""
        cost = calculate_step_cost(
            time_step_hours=0.25,
            soc_wh=5000,
            action_w=0,
            grid_price=0.30,
            feed_in_price=0.07,
            pv_production_w=3000,
            consumption_w=1000,
            rte=0.90,
            degradation_cost_per_kwh=0.03,
            battery_config=battery_config,
        )
        # Net grid = 1000 - 3000 = -2000W (exporting)
        # 2000W * 0.25h = 500 Wh = 0.5 kWh * 0.07 = 0.035 EUR revenue
        assert cost == pytest.approx(-0.035, abs=0.001)

    def test_charging_from_grid(self, battery_config):
        """Charging battery from grid, no PV."""
        cost = calculate_step_cost(
            time_step_hours=0.25,
            soc_wh=5000,
            action_w=2000,  # Charge at 2kW
            grid_price=0.10,
            feed_in_price=0.07,
            pv_production_w=0,
            consumption_w=500,
            rte=0.90,
            degradation_cost_per_kwh=0.03,
            battery_config=battery_config,
        )
        # Grid to battery = 2000 / sqrt(0.90) = ~2108W
        # Net grid = 500 + 2108 = 2608W
        # Grid cost = 2608 * 0.25 / 1000 * 0.10 = 0.0652
        # Degradation = 2000 * 0.25 / 1000 * 0.03 = 0.015
        assert cost > 0.07  # Grid + degradation

    def test_discharging_to_home(self, battery_config):
        """Discharging battery to cover consumption."""
        cost = calculate_step_cost(
            time_step_hours=0.25,
            soc_wh=5000,
            action_w=-2000,  # Discharge at 2kW
            grid_price=0.30,
            feed_in_price=0.07,
            pv_production_w=0,
            consumption_w=2000,
            rte=0.90,
            degradation_cost_per_kwh=0.03,
            battery_config=battery_config,
        )
        # Battery provides usable_power = 2000 * sqrt(0.90) = ~1897W
        # Net grid = 2000 - 0 + (-1897) = 103W (still small import)
        # Should be much cheaper than buying full 2000W from grid
        no_battery_cost = 2000 * 0.25 / 1000 * 0.30  # = 0.15
        assert cost < no_battery_cost

    def test_degradation_cost_added(self, battery_config):
        """Degradation cost is added to total."""
        cost_idle = calculate_step_cost(
            time_step_hours=0.25, soc_wh=5000, action_w=0,
            grid_price=0.10, feed_in_price=0.07,
            pv_production_w=0, consumption_w=0,
            rte=0.90, degradation_cost_per_kwh=0.03,
            battery_config=battery_config,
        )
        cost_charge = calculate_step_cost(
            time_step_hours=0.25, soc_wh=5000, action_w=2000,
            grid_price=0.10, feed_in_price=0.07,
            pv_production_w=0, consumption_w=0,
            rte=0.90, degradation_cost_per_kwh=0.03,
            battery_config=battery_config,
        )
        # Charging adds degradation: 2000 * 0.25 / 1000 * 0.03 = 0.015
        assert cost_charge > cost_idle

    def test_dc_pv_charges_at_higher_efficiency(self, dc_battery_config):
        """DC-coupled PV charges more efficiently than AC."""
        cost_ac = calculate_step_cost(
            time_step_hours=0.25, soc_wh=5000, action_w=2000,
            grid_price=0.30, feed_in_price=0.07,
            pv_production_w=3000, consumption_w=1000,
            rte=0.90, degradation_cost_per_kwh=0.03,
            battery_config=dc_battery_config,
            pv_dc_production_w=0,  # No DC PV
        )
        cost_dc = calculate_step_cost(
            time_step_hours=0.25, soc_wh=5000, action_w=2000,
            grid_price=0.30, feed_in_price=0.07,
            pv_production_w=1000, consumption_w=1000,
            rte=0.90, degradation_cost_per_kwh=0.03,
            battery_config=dc_battery_config,
            pv_dc_production_w=2000,  # 2kW DC PV
        )
        # DC PV charging is "free" (no grid cost), so cost_dc should be lower
        assert cost_dc <= cost_ac

    def test_dc_pv_excess_to_ac(self, dc_battery_config):
        """Excess DC PV goes to AC side through inverter."""
        cost = calculate_step_cost(
            time_step_hours=0.25, soc_wh=5000, action_w=0,
            grid_price=0.30, feed_in_price=0.07,
            pv_production_w=0, consumption_w=1000,
            rte=0.90, degradation_cost_per_kwh=0.03,
            battery_config=dc_battery_config,
            pv_dc_production_w=3000,  # 3kW DC PV, battery idle
        )
        # DC PV excess: 3000W * 0.96 = 2880W to AC
        # Net grid = 1000 - 2880 = -1880W (exporting)
        # Revenue = 1880 * 0.25 / 1000 * 0.07 = 0.0329
        assert cost < 0  # Revenue from export


class TestOptimizeBatterySchedule:
    """Tests for optimize_battery_schedule function."""

    def test_basic_optimization(self, battery_config):
        """Basic optimization with price spread."""
        # Low price then high price -> should charge then discharge
        prices = [0.05, 0.05, 0.30, 0.30]  # EUR/kWh per 15-min step
        pv = [0.0] * 4
        consumption = [0.5] * 4

        result = optimize_battery_schedule(
            battery_config=battery_config,
            current_soc_kwh=5.0,
            price_forecast=prices,
            feed_in_forecast=None,
            pv_forecast=pv,
            consumption_forecast=consumption,
            time_step_minutes=15,
            degradation_cost_per_kwh=0.03,
            min_price_spread=0.05,
        )

        assert isinstance(result, OptimizationResult)
        assert len(result.power_schedule_kw) == 4
        assert len(result.mode_schedule) == 4
        assert len(result.soc_schedule_kwh) == 5  # n+1

    def test_savings_positive_with_price_spread(self, battery_config):
        """Optimizer should find savings when price spread exists."""
        # Alternating low/high prices
        n = 8
        prices = [0.05, 0.05, 0.05, 0.05, 0.30, 0.30, 0.30, 0.30]
        pv = [0.0] * n
        consumption = [0.5] * n

        result = optimize_battery_schedule(
            battery_config=battery_config,
            current_soc_kwh=5.0,
            price_forecast=prices,
            feed_in_forecast=None,
            pv_forecast=pv,
            consumption_forecast=consumption,
            time_step_minutes=15,
        )

        # With such a large price spread, optimizer should find savings
        assert result.savings >= 0

    def test_flat_prices_no_arbitrage(self, battery_config):
        """With flat prices and min SoC, cycling adds cost (no arbitrage)."""
        prices = [0.20] * 8
        pv = [0.0] * 8
        consumption = [0.5] * 8

        result = optimize_battery_schedule(
            battery_config=battery_config,
            current_soc_kwh=battery_config.min_soc_kwh,  # Start at min SoC
            price_forecast=prices,
            feed_in_forecast=None,
            pv_forecast=pv,
            consumption_forecast=consumption,
            time_step_minutes=15,
            degradation_cost_per_kwh=0.03,
        )

        # Starting at min SoC with flat prices: no benefit from cycling
        # (charging then discharging at same price loses RTE + degradation)
        assert result.savings == pytest.approx(0.0, abs=0.01)

    def test_soc_stays_in_bounds(self, battery_config):
        """SoC should never exceed configured bounds."""
        prices = [0.05] * 4 + [0.30] * 4
        pv = [0.0] * 8
        consumption = [0.5] * 8

        result = optimize_battery_schedule(
            battery_config=battery_config,
            current_soc_kwh=5.0,
            price_forecast=prices,
            feed_in_forecast=None,
            pv_forecast=pv,
            consumption_forecast=consumption,
            time_step_minutes=15,
        )

        for soc in result.soc_schedule_kwh:
            assert soc >= battery_config.min_soc_kwh - 0.1  # Small tolerance for discretization
            assert soc <= battery_config.max_soc_kwh + 0.1

    def test_empty_forecast_returns_empty(self, battery_config):
        """Empty input should return empty result."""
        result = optimize_battery_schedule(
            battery_config=battery_config,
            current_soc_kwh=5.0,
            price_forecast=[],
            feed_in_forecast=None,
            pv_forecast=[],
            consumption_forecast=[],
        )

        assert result.optimal_power_kw == 0.0
        assert result.optimal_mode == "idle"
        assert result.savings == 0.0

    def test_mode_schedule_consistency(self, battery_config):
        """Mode schedule should match power schedule."""
        prices = [0.05, 0.05, 0.30, 0.30]
        pv = [0.0] * 4
        consumption = [0.5] * 4

        result = optimize_battery_schedule(
            battery_config=battery_config,
            current_soc_kwh=5.0,
            price_forecast=prices,
            feed_in_forecast=None,
            pv_forecast=pv,
            consumption_forecast=consumption,
            time_step_minutes=15,
        )

        for power, mode in zip(result.power_schedule_kw, result.mode_schedule):
            if power > 0:
                assert mode == "charging"
            elif power < 0:
                assert mode == "discharging"
            else:
                assert mode == "idle"

    def test_dc_pv_forecast_used(self, dc_battery_config):
        """DC PV forecast should be accepted and used."""
        prices = [0.30] * 4
        pv = [0.0] * 4
        consumption = [0.5] * 4
        pv_dc = [2.0, 2.0, 0.0, 0.0]  # 2kW DC PV first 2 steps

        result = optimize_battery_schedule(
            battery_config=dc_battery_config,
            current_soc_kwh=5.0,
            price_forecast=prices,
            feed_in_forecast=None,
            pv_forecast=pv,
            consumption_forecast=consumption,
            time_step_minutes=15,
            pv_dc_forecast=pv_dc,
        )

        assert isinstance(result, OptimizationResult)
        assert len(result.power_schedule_kw) == 4

    def test_feed_in_price_used(self, battery_config):
        """Different feed-in price should affect optimization."""
        prices = [0.30] * 4
        pv = [3.0] * 4  # PV surplus
        consumption = [0.5] * 4

        result_low_feedin = optimize_battery_schedule(
            battery_config=battery_config,
            current_soc_kwh=5.0,
            price_forecast=prices,
            feed_in_forecast=[0.01] * 4,  # Very low feed-in
            pv_forecast=pv,
            consumption_forecast=consumption,
            time_step_minutes=15,
        )

        result_high_feedin = optimize_battery_schedule(
            battery_config=battery_config,
            current_soc_kwh=5.0,
            price_forecast=prices,
            feed_in_forecast=[0.25] * 4,  # High feed-in
            pv_forecast=pv,
            consumption_forecast=consumption,
            time_step_minutes=15,
        )

        # With low feed-in, storing PV is more attractive
        # With high feed-in, exporting is more attractive
        # Costs should differ
        assert result_low_feedin.total_cost != result_high_feedin.total_cost


class TestFindNearestSocIdx:
    """Tests for _find_nearest_soc_idx helper."""

    def test_exact_match(self):
        states = [1000, 2000, 3000, 4000, 5000]
        assert _find_nearest_soc_idx(3000, states) == 2

    def test_between_states(self):
        states = [1000, 2000, 3000, 4000, 5000]
        assert _find_nearest_soc_idx(2400, states) == 1  # Closer to 2000
        assert _find_nearest_soc_idx(2600, states) == 2  # Closer to 3000

    def test_below_range(self):
        states = [1000, 2000, 3000]
        assert _find_nearest_soc_idx(500, states) == 0

    def test_above_range(self):
        states = [1000, 2000, 3000]
        assert _find_nearest_soc_idx(5000, states) == 2


class TestHeuristics:
    """Tests for should_charge_now and should_discharge_now heuristics."""

    def test_charge_on_arbitrage(self):
        should, reason = should_charge_now(0.05, [0.10, 0.20, 0.30], 50.0)
        assert should is True
        assert "arbitrage" in reason

    def test_no_charge_at_high_soc(self):
        should, reason = should_charge_now(0.05, [0.30] * 5, 95.0)
        assert should is False
        assert "soc_high" in reason

    def test_no_charge_without_forecast(self):
        should, reason = should_charge_now(0.05, [], 50.0)
        assert should is False

    def test_discharge_at_high_price(self):
        # Current price is highest -> should discharge
        should, reason = should_discharge_now(0.35, [0.10, 0.15, 0.20], 70.0)
        assert should is True

    def test_no_discharge_at_low_soc(self):
        should, reason = should_discharge_now(0.35, [0.10] * 5, 5.0, min_soc_percent=10.0)
        assert should is False
        assert "soc_low" in reason

    def test_no_discharge_without_forecast(self):
        should, reason = should_discharge_now(0.35, [], 50.0)
        assert should is False

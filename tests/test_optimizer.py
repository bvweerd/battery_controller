"""Tests for optimizer.py."""

import pytest

from custom_components.battery_controller.battery_model import BatteryConfig
import math

from custom_components.battery_controller.optimizer import (
    OptimizationResult,
    calculate_step_cost,
    optimize_battery_schedule,
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
            time_step_hours=0.25,
            soc_wh=5000,
            action_w=0,
            grid_price=0.10,
            feed_in_price=0.07,
            pv_production_w=0,
            consumption_w=0,
            rte=0.90,
            degradation_cost_per_kwh=0.03,
            battery_config=battery_config,
        )
        cost_charge = calculate_step_cost(
            time_step_hours=0.25,
            soc_wh=5000,
            action_w=2000,
            grid_price=0.10,
            feed_in_price=0.07,
            pv_production_w=0,
            consumption_w=0,
            rte=0.90,
            degradation_cost_per_kwh=0.03,
            battery_config=battery_config,
        )
        # Charging adds degradation: 2000 * 0.25 / 1000 * 0.03 = 0.015
        assert cost_charge > cost_idle

    def test_dc_pv_charges_at_higher_efficiency(self, dc_battery_config):
        """DC-coupled PV charges more efficiently than AC."""
        cost_ac = calculate_step_cost(
            time_step_hours=0.25,
            soc_wh=5000,
            action_w=2000,
            grid_price=0.30,
            feed_in_price=0.07,
            pv_production_w=3000,
            consumption_w=1000,
            rte=0.90,
            degradation_cost_per_kwh=0.03,
            battery_config=dc_battery_config,
            pv_dc_production_w=0,  # No DC PV
        )
        cost_dc = calculate_step_cost(
            time_step_hours=0.25,
            soc_wh=5000,
            action_w=2000,
            grid_price=0.30,
            feed_in_price=0.07,
            pv_production_w=1000,
            consumption_w=1000,
            rte=0.90,
            degradation_cost_per_kwh=0.03,
            battery_config=dc_battery_config,
            pv_dc_production_w=2000,  # 2kW DC PV
        )
        # DC PV charging is "free" (no grid cost), so cost_dc should be lower
        assert cost_dc <= cost_ac

    def test_dc_pv_excess_to_ac(self, dc_battery_config):
        """Excess DC PV goes to AC side through inverter."""
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
            assert (
                soc >= battery_config.min_soc_kwh - 0.1
            )  # Small tolerance for discretization
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
        # Test multiple scenarios to ensure all mode types are covered
        scenarios = [
            # Scenario 1: Very low price then high for charging
            {
                "prices": [0.01, 0.45, 0.45, 0.45, 0.45, 0.45],
                "soc": 4.0,
                "degradation": 0.003,
                "min_spread": 0.0,
            },
            # Scenario 2: High price then low for discharging
            {
                "prices": [0.45, 0.01, 0.01, 0.01, 0.01, 0.01],
                "soc": 7.0,
                "degradation": 0.003,
                "min_spread": 0.0,
            },
            # Scenario 3: Flat prices at min SoC for idle mode
            {
                "prices": [0.20] * 4,
                "soc": battery_config.min_soc_kwh,
                "degradation": 0.05,
                "min_spread": 0.0,
            },
        ]

        for scenario in scenarios:
            result = optimize_battery_schedule(
                battery_config=battery_config,
                current_soc_kwh=scenario["soc"],
                price_forecast=scenario["prices"],
                feed_in_forecast=None,
                pv_forecast=[0.0] * len(scenario["prices"]),
                consumption_forecast=[0.5] * len(scenario["prices"]),
                time_step_minutes=15,
                degradation_cost_per_kwh=scenario["degradation"],
                min_price_spread=scenario["min_spread"],
            )

            # Check mode consistency with power
            for power, mode in zip(result.power_schedule_kw, result.mode_schedule):
                if power > 0.01:
                    assert mode == "charging"
                elif power < -0.01:
                    assert mode == "discharging"
                else:
                    assert mode == "idle"

    def test_mode_schedule_all_types(self, battery_config):
        """Explicitly test all three mode types: charging, idle, discharging."""
        # Scenario 1: Very low price followed by very high -> should charge
        result_charge = optimize_battery_schedule(
            battery_config=battery_config,
            current_soc_kwh=5.0,
            price_forecast=[0.02, 0.40, 0.40, 0.40, 0.40, 0.40],
            feed_in_forecast=None,
            pv_forecast=[0.0] * 6,
            consumption_forecast=[0.5] * 6,
            time_step_minutes=15,
            degradation_cost_per_kwh=0.005,
            min_price_spread=0.0,  # Disable min spread check
        )
        # Should charge during low price
        has_charging = any(p > 0.1 for p in result_charge.power_schedule_kw)
        assert (
            has_charging
        ), f"Should have charging mode. Schedule: {result_charge.power_schedule_kw}"

        # Scenario 2: Flat prices, start at min SoC -> should stay idle
        result_idle = optimize_battery_schedule(
            battery_config=battery_config,
            current_soc_kwh=battery_config.min_soc_kwh,
            price_forecast=[0.20] * 4,
            feed_in_forecast=None,
            pv_forecast=[0.0] * 4,
            consumption_forecast=[0.5] * 4,
            time_step_minutes=15,
            degradation_cost_per_kwh=0.05,  # High degradation discourages cycling
            min_price_spread=0.0,
        )
        # Should stay idle (no arbitrage opportunity)
        has_idle = any(abs(p) < 0.01 for p in result_idle.power_schedule_kw)
        assert (
            has_idle
        ), f"Should have idle mode. Schedule: {result_idle.power_schedule_kw}"

        # Scenario 3: Very high price followed by low -> should discharge
        result_discharge = optimize_battery_schedule(
            battery_config=battery_config,
            current_soc_kwh=7.0,  # Start with good SoC
            price_forecast=[0.40, 0.02, 0.02, 0.02, 0.02, 0.02],
            feed_in_forecast=None,
            pv_forecast=[0.0] * 6,
            consumption_forecast=[0.5] * 6,
            time_step_minutes=15,
            degradation_cost_per_kwh=0.005,
            min_price_spread=0.0,
        )
        # Should discharge during high price
        has_discharging = any(p < -0.1 for p in result_discharge.power_schedule_kw)
        assert has_discharging, f"Should have discharging mode. Schedule: {result_discharge.power_schedule_kw}"

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


class TestActionSpace:
    """Tests that the DP action space never exceeds rated max power."""

    def test_charge_actions_within_max(self, battery_config):
        """No charge action should exceed max_charge_power_kw."""
        # Non-round max (e.g. 4600 W is not a multiple of 500)
        config = BatteryConfig(
            capacity_kwh=10.0,
            max_charge_power_kw=4.6,
            max_discharge_power_kw=4.6,
            round_trip_efficiency=0.90,
            min_soc_percent=10.0,
            max_soc_percent=90.0,
        )
        result = optimize_battery_schedule(
            battery_config=config,
            current_soc_kwh=5.0,
            price_forecast=[0.05] * 4 + [0.35] * 4,
            feed_in_forecast=None,
            pv_forecast=[0.0] * 8,
            consumption_forecast=[0.5] * 8,
            time_step_minutes=15,
        )
        for power in result.power_schedule_kw:
            assert power <= config.max_charge_power_kw + 1e-6
            assert power >= -config.max_discharge_power_kw - 1e-6

    def test_schedule_power_bounded(self, battery_config):
        """Scheduled power should never exceed rated limits."""
        result = optimize_battery_schedule(
            battery_config=battery_config,
            current_soc_kwh=5.0,
            price_forecast=[0.02] * 4 + [0.40] * 4,
            feed_in_forecast=None,
            pv_forecast=[0.0] * 8,
            consumption_forecast=[0.5] * 8,
            time_step_minutes=15,
            degradation_cost_per_kwh=0.001,
            min_price_spread=0.0,
        )
        for power in result.power_schedule_kw:
            assert power <= battery_config.max_charge_power_kw + 1e-6
            assert power >= -battery_config.max_discharge_power_kw - 1e-6


class TestOscillationFilterFormula:
    """Tests that min_arbitrage_spread uses the correct formula."""

    def test_min_spread_consistent_with_rte(self, battery_config):
        """With large enough price spread, arbitrage should be allowed despite RTE losses."""
        # With RTE=0.90, sqrt_rte≈0.9487
        # min_arbitrage_spread = (2*0.03 + 0.05) / 0.9487 ≈ 0.116
        # So a 0.20 spread (0.30 - 0.10) should allow arbitrage
        rte = battery_config.round_trip_efficiency
        sqrt_rte = math.sqrt(rte)
        deg = 0.03
        min_spread = 0.05
        expected_threshold = (2 * deg + min_spread) / sqrt_rte

        result = optimize_battery_schedule(
            battery_config=battery_config,
            current_soc_kwh=5.0,
            price_forecast=[0.10] * 4 + [0.30] * 4,
            feed_in_forecast=None,
            pv_forecast=[0.0] * 8,
            consumption_forecast=[0.5] * 8,
            time_step_minutes=15,
            degradation_cost_per_kwh=deg,
            min_price_spread=min_spread,
        )
        # Price spread = 0.20 > expected_threshold ≈ 0.116 → arbitrage expected
        assert any(
            m == "charging" for m in result.mode_schedule[:4]
        ), f"Expected charging; threshold={expected_threshold:.3f}, spread=0.20"

    def test_spread_below_threshold_no_arbitrage(self, battery_config):
        """Price spread below corrected threshold should produce no arbitrage."""
        rte = battery_config.round_trip_efficiency
        sqrt_rte = math.sqrt(rte)
        deg = 0.03
        min_spread = 0.05
        threshold = (2 * deg + min_spread) / sqrt_rte  # ≈ 0.116
        # Use a spread just below the threshold
        low_price = 0.20
        high_price = low_price + threshold * 0.5  # well below threshold

        result = optimize_battery_schedule(
            battery_config=battery_config,
            current_soc_kwh=5.0,
            price_forecast=[low_price] * 4 + [high_price] * 4,
            feed_in_forecast=None,
            pv_forecast=[0.0] * 8,
            consumption_forecast=[0.5] * 8,
            time_step_minutes=15,
            degradation_cost_per_kwh=deg,
            min_price_spread=min_spread,
        )
        has_charge_then_discharge = any(
            result.mode_schedule[i] == "charging"
            and any(
                result.mode_schedule[j] == "discharging" for j in range(i + 1, i + 8)
            )
            for i in range(len(result.mode_schedule))
        )
        assert not has_charge_then_discharge, "Should not arbitrage with tiny spread"


class TestOscillationPrevention:
    """Tests for oscillation prevention in optimizer."""

    def test_no_oscillation_with_small_price_differences(self, battery_config):
        """Optimizer should not oscillate when price differences are too small."""
        # Small price variations (not enough for profitable arbitrage)
        # RTE=0.9, degradation=0.03, min_spread=0.05
        # Need ~0.15 EUR/kWh spread for profitability
        price_forecast = [0.25, 0.25, 0.24, 0.24, 0.26, 0.26, 0.25, 0.25] * 4
        pv_forecast = [0.0] * 32  # No PV
        consumption_forecast = [0.5] * 32  # Constant load

        result = optimize_battery_schedule(
            battery_config=battery_config,
            current_soc_kwh=5.0,
            price_forecast=price_forecast,
            feed_in_forecast=None,
            pv_forecast=pv_forecast,
            consumption_forecast=consumption_forecast,
            time_step_minutes=15,
            degradation_cost_per_kwh=0.03,
            min_price_spread=0.05,
        )

        # Count mode switches
        mode_switches = 0
        for i in range(len(result.mode_schedule) - 1):
            current = result.mode_schedule[i]
            next_mode = result.mode_schedule[i + 1]
            if (current == "charging" and next_mode == "discharging") or (
                current == "discharging" and next_mode == "charging"
            ):
                mode_switches += 1

        # Should have very few or no switches with such small price variations
        assert mode_switches <= 2, f"Too many mode switches: {mode_switches}"

    def test_allows_profitable_arbitrage(self, battery_config):
        """Optimizer should still allow arbitrage when profitable."""
        # Large price difference: cheap night, expensive peak
        price_forecast = [0.10, 0.10, 0.10, 0.10, 0.35, 0.35, 0.35, 0.35] * 2
        pv_forecast = [0.0] * 16
        consumption_forecast = [0.5] * 16

        result = optimize_battery_schedule(
            battery_config=battery_config,
            current_soc_kwh=5.0,
            price_forecast=price_forecast,
            feed_in_forecast=None,
            pv_forecast=pv_forecast,
            consumption_forecast=consumption_forecast,
            time_step_minutes=15,
            degradation_cost_per_kwh=0.03,
            min_price_spread=0.05,
        )

        # Should charge during cheap periods
        assert any(mode == "charging" for mode in result.mode_schedule[:4])
        # Should discharge during expensive periods
        assert any(mode == "discharging" for mode in result.mode_schedule[4:8])

    def test_allows_pv_arbitrage_with_feed_in(self, battery_config):
        """Optimizer should charge during PV when can't discharge enough beforehand."""
        # Low starting SoC scenario: can't discharge much in morning,
        # so charging during PV for evening discharge becomes optimal
        grid_price = [0.24] * 4 + [0.25] * 4 + [0.30] * 8  # Evening expensive
        feed_in_price = [0.07] * 16  # Low feed-in price

        # PV surplus in middle period
        pv_forecast = [0.0] * 4 + [2.0] * 4 + [0.0] * 8  # 2kW PV midday
        consumption_forecast = [0.5] * 16  # 0.5kW constant load

        result = optimize_battery_schedule(
            battery_config=battery_config,
            current_soc_kwh=1.5,  # Very low SoC - can't discharge much in morning
            price_forecast=grid_price,
            feed_in_forecast=feed_in_price,
            pv_forecast=pv_forecast,
            consumption_forecast=consumption_forecast,
            time_step_minutes=15,
            degradation_cost_per_kwh=0.03,
            min_price_spread=0.05,
        )

        # Should charge during PV surplus (steps 4-7)
        charge_count = sum(
            1 for mode in result.mode_schedule[4:8] if mode == "charging"
        )

        # Should discharge during evening high prices (steps 8-15)
        discharge_count = sum(
            1 for mode in result.mode_schedule[8:] if mode == "discharging"
        )

        assert charge_count > 0, "Should charge during PV surplus to use later"
        assert discharge_count > 0, "Should discharge during expensive evening"

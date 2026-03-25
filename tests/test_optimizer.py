"""Tests for optimizer.py."""

import pytest

from custom_components.battery_controller.battery_model import BatteryConfig
import math

from custom_components.battery_controller.optimizer import (
    OptimizationResult,
    calculate_step_cost,
    optimize_battery_schedule,
    _find_nearest_soc_idx,
    _filter_micro_cycles,
    _filter_oscillations,
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
        """DC-coupled PV charging avoids grid draw when DC PV covers the full action.

        With action_w = AC setpoint model:
        - AC case: 2000W action with no PV → full 2000W drawn from grid
        - DC case: 2000W action with 2200W DC PV (> action_w / dc_eff = 2062W) →
          dc_charge_w covers the full action, ac_charge_w = 0 → no grid draw for charging
        The DC case has lower grid cost because the full charge comes from DC PV.
        """
        cost_ac = calculate_step_cost(
            time_step_hours=0.25,
            soc_wh=5000,
            action_w=2000,
            grid_price=0.30,
            feed_in_price=0.07,
            pv_production_w=0,
            consumption_w=1000,
            rte=0.90,
            degradation_cost_per_kwh=0.03,
            battery_config=dc_battery_config,
            pv_dc_production_w=0,  # No PV — pure grid charging
        )
        cost_dc = calculate_step_cost(
            time_step_hours=0.25,
            soc_wh=5000,
            action_w=2000,
            grid_price=0.30,
            feed_in_price=0.07,
            pv_production_w=0,
            consumption_w=1000,
            rte=0.90,
            degradation_cost_per_kwh=0.03,
            battery_config=dc_battery_config,
            pv_dc_production_w=2200,  # > action_w / dc_eff: DC PV fully covers the charge
        )
        # DC PV covers full charge → no grid draw for battery; AC case draws 2000W from grid
        assert cost_dc <= cost_ac

    def test_dc_pv_passive_charge_when_idle(self, dc_battery_config):
        """In idle mode, DC-coupled inverters passively charge the battery from DC PV.

        P1.1: action_w=0 (no explicit AC command) does NOT route DC PV to AC when
        the battery has headroom. The MPPT charger absorbs DC PV into the battery.
        Net grid reflects consumption only (DC PV stored, not exported).
        """
        cost = calculate_step_cost(
            time_step_hours=0.25,
            soc_wh=5000,  # mid SoC, plenty of headroom (max=9000 Wh)
            action_w=0,
            grid_price=0.30,
            feed_in_price=0.07,
            pv_production_w=0,
            consumption_w=1000,
            rte=0.90,
            degradation_cost_per_kwh=0.03,
            battery_config=dc_battery_config,
            pv_dc_production_w=3000,  # 3 kW DC PV absorbed into battery passively
        )
        # DC PV goes to battery (passive charge), not to AC.
        # Net grid = 1000 W (consumption only) → importing from grid.
        # Cost = 1000 * 0.25 / 1000 * 0.30 + degradation > 0
        assert cost > 0

    def test_dc_pv_excess_to_ac_when_battery_full(self, dc_battery_config):
        """DC PV routes to AC only when the battery is at max SoC (no headroom)."""
        cost = calculate_step_cost(
            time_step_hours=0.25,
            soc_wh=9000,  # max SoC — no headroom for passive charging
            action_w=0,
            grid_price=0.30,
            feed_in_price=0.07,
            pv_production_w=0,
            consumption_w=1000,
            rte=0.90,
            degradation_cost_per_kwh=0.03,
            battery_config=dc_battery_config,
            pv_dc_production_w=3000,  # 3 kW DC PV, battery full → goes to AC
        )
        # Battery full: all DC PV → AC: 3000 * 0.96 = 2880 W
        # Net grid = 1000 - 2880 = -1880 W (exporting) → revenue
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
            step_durations_hours=None,  # uses 0.25h default
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
            step_durations_hours=None,  # uses 0.25h default
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
            step_durations_hours=None,  # uses 0.25h default
            degradation_cost_per_kwh=0.03,
        )

        # Starting at min SoC with flat prices: no benefit from cycling
        # (charging then discharging at same price loses RTE + degradation)
        assert result.savings == pytest.approx(0.0, abs=0.01)

    def test_idle_battery_with_soc_above_min_savings_zero(self, battery_config):
        """Savings must be 0 when battery is idle, regardless of initial SoC.

        Regression: previously the terminal value of pre-stored energy leaked
        into the savings figure (e.g. showing €0.07 for 1 kWh at feed_in=0.07
        even though the battery did nothing).

        Setup: buy price = feed_in price = terminal_price = 0.07 so that any
        action loses money due to round-trip efficiency losses and degradation.
        Battery is forced idle, and savings must be 0.
        """
        # prices = feed_in = terminal_price: neither charging nor discharging is
        # profitable (efficiency losses + degradation always outweigh the spread)
        prices = [0.07] * 8
        feed_in = [0.07] * 8
        pv = [0.0] * 8
        consumption = [0.5] * 8
        # Start well above min_soc so there IS pre-existing terminal value
        mid_soc = (battery_config.min_soc_kwh + battery_config.max_soc_kwh) / 2.0

        result = optimize_battery_schedule(
            battery_config=battery_config,
            current_soc_kwh=mid_soc,
            price_forecast=prices,
            feed_in_forecast=feed_in,
            pv_forecast=pv,
            consumption_forecast=consumption,
            step_durations_hours=None,  # uses 0.25h default
            degradation_cost_per_kwh=0.03,
        )

        # Battery idle throughout → savings must be 0, not a positive phantom value
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
            step_durations_hours=None,  # uses 0.25h default
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
                step_durations_hours=None,  # uses 0.25h default
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
            step_durations_hours=None,  # uses 0.25h default
            degradation_cost_per_kwh=0.005,
            min_price_spread=0.0,  # Disable min spread check
        )
        # Should charge during low price
        has_charging = any(p > 0.1 for p in result_charge.power_schedule_kw)
        assert has_charging, (
            f"Should have charging mode. Schedule: {result_charge.power_schedule_kw}"
        )

        # Scenario 2: Flat prices, start at min SoC -> should stay idle
        result_idle = optimize_battery_schedule(
            battery_config=battery_config,
            current_soc_kwh=battery_config.min_soc_kwh,
            price_forecast=[0.20] * 4,
            feed_in_forecast=None,
            pv_forecast=[0.0] * 4,
            consumption_forecast=[0.5] * 4,
            step_durations_hours=None,  # uses 0.25h default
            degradation_cost_per_kwh=0.05,  # High degradation discourages cycling
            min_price_spread=0.0,
        )
        # Should stay idle (no arbitrage opportunity)
        has_idle = any(abs(p) < 0.01 for p in result_idle.power_schedule_kw)
        assert has_idle, (
            f"Should have idle mode. Schedule: {result_idle.power_schedule_kw}"
        )

        # Scenario 3: Very high price followed by low -> should discharge
        result_discharge = optimize_battery_schedule(
            battery_config=battery_config,
            current_soc_kwh=7.0,  # Start with good SoC
            price_forecast=[0.40, 0.02, 0.02, 0.02, 0.02, 0.02],
            feed_in_forecast=None,
            pv_forecast=[0.0] * 6,
            consumption_forecast=[0.5] * 6,
            step_durations_hours=None,  # uses 0.25h default
            degradation_cost_per_kwh=0.005,
            min_price_spread=0.0,
        )
        # Should discharge during high price
        has_discharging = any(p < -0.1 for p in result_discharge.power_schedule_kw)
        assert has_discharging, (
            f"Should have discharging mode. Schedule: {result_discharge.power_schedule_kw}"
        )

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
            step_durations_hours=None,  # uses 0.25h default
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
            step_durations_hours=None,  # uses 0.25h default
        )

        result_high_feedin = optimize_battery_schedule(
            battery_config=battery_config,
            current_soc_kwh=5.0,
            price_forecast=prices,
            feed_in_forecast=[0.25] * 4,  # High feed-in
            pv_forecast=pv,
            consumption_forecast=consumption,
            step_durations_hours=None,  # uses 0.25h default
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
            step_durations_hours=None,  # uses 0.25h default
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
            step_durations_hours=None,  # uses 0.25h default
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
            step_durations_hours=None,  # uses 0.25h default
            degradation_cost_per_kwh=deg,
            min_price_spread=min_spread,
        )
        # Price spread = 0.20 > expected_threshold ≈ 0.116 → arbitrage expected
        assert any(m == "charging" for m in result.mode_schedule[:4]), (
            f"Expected charging; threshold={expected_threshold:.3f}, spread=0.20"
        )

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
        # Use a low fixed feed-in price so the terminal condition does not
        # create spurious arbitrage incentive (terminal_price = feed_in[-1]).
        feed_in = [0.07] * 8

        result = optimize_battery_schedule(
            battery_config=battery_config,
            current_soc_kwh=5.0,
            price_forecast=[low_price] * 4 + [high_price] * 4,
            feed_in_forecast=feed_in,
            pv_forecast=[0.0] * 8,
            consumption_forecast=[0.5] * 8,
            step_durations_hours=None,  # uses 0.25h default
            degradation_cost_per_kwh=deg,
            min_price_spread=min_spread,
        )
        n = len(result.mode_schedule)
        has_charge_then_discharge = any(
            result.mode_schedule[i] == "charging"
            and any(
                result.mode_schedule[j] == "discharging"
                for j in range(i + 1, min(i + 8, n))
            )
            for i in range(n)
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
            step_durations_hours=None,  # uses 0.25h default
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
            step_durations_hours=None,  # uses 0.25h default
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
            step_durations_hours=None,  # uses 0.25h default
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

    def test_oscillation_filter_uses_feed_in_for_export_value(self, battery_config):
        """Export-only discharge should be valued at feed-in, not buy price."""
        power, mode, soc = _filter_oscillations(
            power_schedule_kw=[1.0, -1.0],
            mode_schedule=["charging", "discharging"],
            initial_soc_kwh=5.0,
            price_forecast=[0.30, 0.30],
            min_price_spread=0.05,
            degradation_cost_per_kwh=0.03,
            rte=battery_config.round_trip_efficiency,
            step_durations_hours=[0.25, 0.25],
            min_soc_kwh=battery_config.min_soc_kwh,
            max_soc_kwh=battery_config.max_soc_kwh,
            pv_forecast=[2.0, 0.0],
            consumption_forecast=[0.5, 0.0],
            feed_in_forecast=[0.05, 0.05],
        )

        assert mode[0] == "idle"
        assert power[0] == 0.0
        assert soc[1] == 5.0


class TestQuarterHourOscillationFilter:
    """Regression tests for oscillation filter with quarter-hour price data.

    Bug: with 15-min price data, step_durations_hours[0] can be very short
    (e.g. 1 min when the optimizer runs just before a price boundary).  Using
    it as ref_step_h inflated lookahead_steps up to 120, causing the filter to
    scan the entire 36-h horizon and incorrectly pair charge steps with distant
    unrelated discharges — removing profitable charge blocks so only single
    15-min slots survived.

    Additionally, the filter only evaluated the *nearest* discharge in the
    window.  An intermediate low-spread discharge would wrongly suppress a
    charge whose actual profitable match was slightly further in the window.
    """

    def test_charge_block_preserved_with_short_first_step(self):
        """Charge block must not be removed when first step is very short (1 min).

        Simulates the optimizer running 1 minute before a price boundary.
        Before the fix, lookahead_steps = round(2.0 / 0.017) = 120 which
        covered the entire 96-step horizon, causing charge blocks to be
        paired with distant, low-spread discharges and incorrectly removed.
        """
        from custom_components.battery_controller.optimizer import _filter_oscillations

        n = 16
        # Steps 0-3: cheap (4 ct), steps 4-7: idle, steps 8-15: expensive (25 ct)
        prices = [0.04] * 4 + [0.14] * 4 + [0.25] * 8
        power = [3.0] * 4 + [0.0] * 4 + [-3.0] * 8
        mode = ["charging"] * 4 + ["idle"] * 4 + ["discharging"] * 8

        # First step is 1 minute (= 0.017 h); rest are full 15-min steps
        step_durations = [1 / 60] + [0.25] * (n - 1)

        filtered_power, filtered_mode, _ = _filter_oscillations(
            power_schedule_kw=power,
            mode_schedule=mode,
            initial_soc_kwh=2.0,
            price_forecast=prices,
            min_price_spread=0.05,
            degradation_cost_per_kwh=0.03,
            rte=0.9,
            step_durations_hours=step_durations,
            min_soc_kwh=1.0,
            max_soc_kwh=10.0,
        )

        # Profitable charge steps (spread = 25 - 4/0.9 ≈ 20.6 ct >> threshold)
        # must not be suppressed regardless of the short first step.
        assert filtered_mode[0] == "charging", "charge step 0 wrongly suppressed"
        assert filtered_mode[1] == "charging", "charge step 1 wrongly suppressed"
        assert filtered_mode[2] == "charging", "charge step 2 wrongly suppressed"
        assert filtered_mode[3] == "charging", "charge step 3 wrongly suppressed"

    def test_charge_block_preserved_with_intermediate_low_spread_discharge(self):
        """Charge block must survive when an intermediate nearby discharge has low spread.

        Before the fix the filter broke on the *first* discharge found.  An
        intermediate discharge at a small spread caused the charge to be
        removed, even though a profitable discharge appeared a few steps later
        within the same lookahead window.
        """
        from custom_components.battery_controller.optimizer import _filter_oscillations

        n = 16
        # Step 0: charge at 4 ct
        # Step 2: small discharge at 7 ct (spread 7-4/0.9 ≈ 2.6 ct < threshold)
        # Step 8: big discharge at 25 ct (spread 25-4/0.9 ≈ 20.6 ct >> threshold)
        prices = [0.04, 0.04, 0.07, 0.04, 0.04, 0.04, 0.04, 0.04, 0.25] + [0.25] * 7
        power = [3.0, 3.0, -1.0, 0.0, 0.0, 0.0, 0.0, 0.0] + [-3.0] * 8
        mode = (
            ["charging", "charging", "discharging"] + ["idle"] * 5 + ["discharging"] * 8
        )
        step_durations = [0.25] * n

        filtered_power, filtered_mode, _ = _filter_oscillations(
            power_schedule_kw=power,
            mode_schedule=mode,
            initial_soc_kwh=2.0,
            price_forecast=prices,
            min_price_spread=0.05,
            degradation_cost_per_kwh=0.03,
            rte=0.9,
            step_durations_hours=step_durations,
            min_soc_kwh=1.0,
            max_soc_kwh=10.0,
        )

        # The charge steps 0 and 1 have a profitable discharge at step 8
        # (spread ≈ 20.6 ct >> 11.6 ct threshold). They must be kept even
        # though the nearest discharge at step 2 has an insufficient spread.
        assert filtered_mode[0] == "charging", (
            "charge step 0 wrongly suppressed by intermediate low-spread discharge"
        )
        assert filtered_mode[1] == "charging", (
            "charge step 1 wrongly suppressed by intermediate low-spread discharge"
        )

    def test_true_oscillation_still_removed(self):
        """Rapid charge→discharge with no profitable match must still be suppressed."""
        from custom_components.battery_controller.optimizer import _filter_oscillations

        n = 8
        # All discharges have very small spread vs the charges → true oscillation
        prices = [0.04, 0.05, 0.04, 0.05, 0.04, 0.05, 0.04, 0.05]
        power = [3.0, -1.0, 3.0, -1.0, 3.0, -1.0, 3.0, -1.0]
        mode = ["charging", "discharging"] * 4
        step_durations = [0.25] * n

        filtered_power, filtered_mode, _ = _filter_oscillations(
            power_schedule_kw=power,
            mode_schedule=mode,
            initial_soc_kwh=5.0,
            price_forecast=prices,
            min_price_spread=0.05,
            degradation_cost_per_kwh=0.03,
            rte=0.9,
            step_durations_hours=step_durations,
            min_soc_kwh=1.0,
            max_soc_kwh=10.0,
        )

        charge_count = sum(1 for m in filtered_mode if m == "charging")
        assert charge_count == 0, (
            f"True oscillations should be suppressed, but {charge_count} charge steps remain"
        )


class TestMicroCycleFilter:
    """Tests for micro-cycle post-processing."""

    def test_micro_cycle_filter_recomputes_soc_schedule(self, battery_config):
        """Removing a micro-cycle must also remove its SoC movement."""
        power, mode, soc = _filter_micro_cycles(
            power_schedule_kw=[0.1, 0.0],
            mode_schedule=["charging", "idle"],
            initial_soc_kwh=5.0,
            step_durations_hours=[0.25, 0.25],
            rte=battery_config.round_trip_efficiency,
            min_soc_kwh=battery_config.min_soc_kwh,
            max_soc_kwh=battery_config.max_soc_kwh,
            min_cycle_kwh=0.2,
        )

        assert power == [0.0, 0.0]
        assert mode == ["idle", "idle"]
        assert soc == [5.0, 5.0, 5.0]


class TestTerminalShadowPrice:
    """Tests for terminal_shadow_price parameter."""

    def _base_args(self, battery_config):
        return dict(
            battery_config=battery_config,
            current_soc_kwh=5.0,
            pv_forecast=[0.0] * 8,
            consumption_forecast=[0.5] * 8,
            step_durations_hours=[0.25] * 8,
            degradation_cost_per_kwh=0.03,
            min_price_spread=0.05,
        )

    def test_shadow_price_used_as_terminal(self, battery_config):
        """High shadow price should encourage charging; low should encourage discharging."""
        # Flat prices — no arbitrage opportunity; the terminal price drives the decision.
        flat_price = [0.20] * 8
        feed_in = [0.07] * 8
        args = self._base_args(battery_config)

        # High terminal shadow price: stored energy is worth a lot → prefer charging
        result_high = optimize_battery_schedule(
            price_forecast=flat_price,
            feed_in_forecast=feed_in,
            terminal_shadow_price=0.40,
            **args,
        )

        # Low terminal shadow price: stored energy worth little → prefer discharging
        result_low = optimize_battery_schedule(
            price_forecast=flat_price,
            feed_in_forecast=feed_in,
            terminal_shadow_price=0.01,
            **args,
        )

        final_soc_high = result_high.soc_schedule_kwh[-1]
        final_soc_low = result_low.soc_schedule_kwh[-1]
        assert final_soc_high > final_soc_low, (
            "High terminal shadow price should lead to higher end SoC than low"
        )

    def test_fallback_when_no_shadow_price(self, battery_config):
        """Without shadow price, optimizer uses feed-in tail average as terminal."""
        price = [0.10, 0.10, 0.10, 0.30, 0.30, 0.30, 0.30, 0.30]
        feed_in = [0.07] * 8
        args = self._base_args(battery_config)

        result = optimize_battery_schedule(
            price_forecast=price,
            feed_in_forecast=feed_in,
            terminal_shadow_price=None,
            **args,
        )
        # Should return a valid result with no crash
        assert len(result.power_schedule_kw) == 8
        assert result.shadow_price_eur_kwh >= 0.0

    def test_negative_shadow_price_ignored(self, battery_config):
        """Negative shadow price should be ignored; fallback to feed-in tail."""
        price = [0.20] * 8
        feed_in = [0.07] * 8
        args = self._base_args(battery_config)

        # With None (baseline fallback)
        result_none = optimize_battery_schedule(
            price_forecast=price,
            feed_in_forecast=feed_in,
            terminal_shadow_price=None,
            **args,
        )

        # Negative value should produce identical result to None
        result_neg = optimize_battery_schedule(
            price_forecast=price,
            feed_in_forecast=feed_in,
            terminal_shadow_price=-0.10,
            **args,
        )

        assert result_none.power_schedule_kw == result_neg.power_schedule_kw

    def test_shadow_price_self_consistency(self, battery_config):
        """Shadow price fed back as terminal should be close to the new shadow price."""
        # Stable price scenario: shadow price should converge after one round-trip.
        price = [0.10, 0.10, 0.20, 0.20, 0.10, 0.10, 0.20, 0.20]
        feed_in = [0.07] * 8
        args = self._base_args(battery_config)

        # Run 1: no prior shadow price
        result1 = optimize_battery_schedule(
            price_forecast=price,
            feed_in_forecast=feed_in,
            terminal_shadow_price=None,
            **args,
        )
        lambda1 = result1.shadow_price_eur_kwh

        # Run 2: feed shadow price from run 1 back as terminal condition
        result2 = optimize_battery_schedule(
            price_forecast=price,
            feed_in_forecast=feed_in,
            terminal_shadow_price=lambda1,
            **args,
        )
        lambda2 = result2.shadow_price_eur_kwh

        # Shadow price should not explode — stay within a reasonable range of the prices
        max_price = max(price)
        assert 0.0 <= lambda2 <= max_price * 2, (
            f"Shadow price {lambda2} after feedback is outside reasonable bounds"
        )


class TestReportedMetrics:
    """Tests for raw vs post-processed optimizer metrics."""

    def test_result_exposes_raw_dp_metrics(self, battery_config):
        """Optimizer should expose raw DP metrics separately from reported totals."""
        result = optimize_battery_schedule(
            battery_config=battery_config,
            current_soc_kwh=5.0,
            price_forecast=[0.10] * 4 + [0.30] * 4,
            feed_in_forecast=[0.07] * 8,
            pv_forecast=[0.0] * 8,
            consumption_forecast=[0.5] * 8,
            step_durations_hours=[0.25] * 8,
            degradation_cost_per_kwh=0.03,
            min_price_spread=0.05,
        )

        assert result.raw_total_cost is not None
        assert result.raw_savings is not None
        # Shadow price is always the raw DP value (no post-processed variant)
        assert result.shadow_price_eur_kwh != 0.0

    def test_raw_vs_processed_cost_no_filter_delta(self, battery_config):
        """When no actions are filtered, raw and processed costs should nearly match."""
        # Use flat prices so oscillation/micro-cycle filters won't trigger
        result = optimize_battery_schedule(
            battery_config=battery_config,
            current_soc_kwh=5.0,
            price_forecast=[0.20] * 8,
            feed_in_forecast=[0.07] * 8,
            pv_forecast=[0.0] * 8,
            consumption_forecast=[0.5] * 8,
            step_durations_hours=[0.25] * 8,
            degradation_cost_per_kwh=0.03,
            min_price_spread=0.05,
        )

        # With flat prices and no PV, the optimizer should idle (no arbitrage).
        # Raw DP cost and post-processed cost should be very close.
        assert result.raw_total_cost is not None
        assert abs(result.raw_total_cost - result.total_cost) < 0.01


class TestOscillationFilterDcPv:
    """Tests for DC PV interaction with the oscillation filter."""

    def test_oscillation_filter_dc_pv_passive_charge(self):
        """Oscillation filter should account for passive DC PV when evaluating charge cost."""
        from custom_components.battery_controller.optimizer import _filter_oscillations

        # Scenario: charge at step 0, discharge at step 1. With DC PV,
        # the charge at step 0 is partially free (passive DC PV charging).
        # The filter should be less aggressive about removing this pair.
        power_schedule = [1.0, -1.0, 0.0, 0.0]
        mode_schedule = ["charging", "discharging", "idle", "idle"]
        initial_soc = 1.0

        # Prices: charge at 0.10, discharge at 0.15 — tight spread
        prices = [0.10, 0.15, 0.10, 0.10]
        feed_in = [0.07, 0.07, 0.07, 0.07]
        step_durations = [0.25] * 4
        pv_forecast = [0.0] * 4
        consumption_forecast = [0.0] * 4

        # Without DC PV: this pair should be filtered (spread too small)
        result_no_dc = _filter_oscillations(
            power_schedule_kw=list(power_schedule),
            mode_schedule=list(mode_schedule),
            initial_soc_kwh=initial_soc,
            price_forecast=prices,
            min_price_spread=0.02,
            degradation_cost_per_kwh=0.03,
            rte=0.90,
            step_durations_hours=step_durations,
            min_soc_kwh=0.5,
            max_soc_kwh=5.0,
            pv_forecast=pv_forecast,
            consumption_forecast=consumption_forecast,
            feed_in_forecast=feed_in,
            pv_dc_forecast=[0.0] * 4,
            pv_dc_coupled=False,
        )

        # With DC PV: passive charge reduces effective cost, making the pair more viable
        result_with_dc = _filter_oscillations(
            power_schedule_kw=list(power_schedule),
            mode_schedule=list(mode_schedule),
            initial_soc_kwh=initial_soc,
            price_forecast=prices,
            min_price_spread=0.02,
            degradation_cost_per_kwh=0.03,
            rte=0.90,
            step_durations_hours=step_durations,
            min_soc_kwh=0.5,
            max_soc_kwh=5.0,
            pv_forecast=pv_forecast,
            consumption_forecast=consumption_forecast,
            feed_in_forecast=feed_in,
            pv_dc_forecast=[2.0, 0.0, 0.0, 0.0],  # 2 kW DC PV during charge step
            pv_dc_coupled=True,
            pv_dc_efficiency=0.97,
        )

        # With DC PV, the charge cost is lower (partially free), so the filter
        # should be less likely to suppress the charge step.
        # At minimum, verify the DC PV case preserves more charging than without.
        no_dc_charge_steps = sum(1 for m in result_no_dc[1] if m == "charging")
        dc_charge_steps = sum(1 for m in result_with_dc[1] if m == "charging")
        assert dc_charge_steps >= no_dc_charge_steps


class TestSocDependentDerating:
    """Tests for SoC-dependent power derating in the DP."""

    def _marstek_config(self) -> BatteryConfig:
        """Marstek Venus A-like config: 5.2 kWh, 1.2 kW nominal, derating near extremes."""
        return BatteryConfig(
            capacity_kwh=5.2,
            max_charge_power_kw=1.2,
            max_discharge_power_kw=1.2,
            round_trip_efficiency=0.92,
            min_soc_percent=10.0,  # min = 0.52 kWh
            max_soc_percent=100.0,  # max = 5.2 kWh
            high_soc_charge_threshold_pct=95.0,  # above 4.94 kWh → 0.45 kW
            high_soc_max_charge_kw=0.45,
            low_soc_discharge_threshold_pct=15.0,  # below 0.78 kWh → 0.38 kW
            low_soc_max_discharge_kw=0.38,
        )

    def test_charge_power_limited_above_threshold(self):
        """When starting above the high-SoC charge threshold, charge power is capped."""
        config = self._marstek_config()
        # Start at 97% SoC (above 95% threshold) with a low buy price
        start_soc_kwh = 5.044  # 97%
        prices = [0.05, 0.05, 0.30, 0.30]
        result = optimize_battery_schedule(
            battery_config=config,
            current_soc_kwh=start_soc_kwh,
            price_forecast=prices,
            feed_in_forecast=None,
            pv_forecast=[0.0] * 4,
            consumption_forecast=[0.2] * 4,
            step_durations_hours=[0.25] * 4,
            degradation_cost_per_kwh=0.02,
            min_price_spread=0.05,
        )
        # Any charge step at this SoC must stay at or below the derated limit (0.45 kW)
        for i, (mode, power) in enumerate(
            zip(result.mode_schedule, result.power_schedule_kw)
        ):
            if mode == "charging" and result.soc_schedule_kwh[i] >= 5.2 * 0.95:
                assert power <= pytest.approx(0.45 + 0.01), (
                    f"step {i}: charge power {power:.3f} kW exceeds derated limit 0.45 kW"
                )

    def test_discharge_power_limited_below_threshold(self):
        """When starting below the low-SoC discharge threshold, discharge power is capped."""
        config = self._marstek_config()
        # Start at 12% SoC (below 15% threshold)
        start_soc_kwh = 0.624  # 12%
        prices = [0.30, 0.30, 0.05, 0.05]
        result = optimize_battery_schedule(
            battery_config=config,
            current_soc_kwh=start_soc_kwh,
            price_forecast=prices,
            feed_in_forecast=None,
            pv_forecast=[0.0] * 4,
            consumption_forecast=[0.2] * 4,
            step_durations_hours=[0.25] * 4,
            degradation_cost_per_kwh=0.02,
            min_price_spread=0.05,
        )
        # Any discharge step at this SoC must not exceed derated limit (0.38 kW)
        for i, (mode, power) in enumerate(
            zip(result.mode_schedule, result.power_schedule_kw)
        ):
            if mode == "discharging" and result.soc_schedule_kwh[i] <= 5.2 * 0.15:
                assert abs(power) <= pytest.approx(0.38 + 0.01), (
                    f"step {i}: discharge power {abs(power):.3f} kW exceeds derated limit 0.38 kW"
                )

    def test_full_power_available_at_mid_soc(self):
        """Away from extremes, full nominal power is available."""
        config = self._marstek_config()
        # Start at 50% SoC — well away from both derating zones
        start_soc_kwh = 2.6  # 50%
        # Flat low prices first, then high — should charge then discharge at full power
        prices = [0.05, 0.05, 0.05, 0.05, 0.30, 0.30, 0.30, 0.30]
        result = optimize_battery_schedule(
            battery_config=config,
            current_soc_kwh=start_soc_kwh,
            price_forecast=prices,
            feed_in_forecast=None,
            pv_forecast=[0.0] * 8,
            consumption_forecast=[0.1] * 8,
            step_durations_hours=[0.25] * 8,
            degradation_cost_per_kwh=0.02,
            min_price_spread=0.05,
        )
        # At mid-SoC at least one charging step should use close to full power (1.2 kW)
        charge_powers = [
            p
            for i, (m, p) in enumerate(
                zip(result.mode_schedule, result.power_schedule_kw)
            )
            if m == "charging" and result.soc_schedule_kwh[i] < 5.2 * 0.95
        ]
        if charge_powers:
            assert max(charge_powers) >= 0.8

    def test_derating_does_not_affect_unrelated_config(self):
        """A config without derating produces the same result as before this feature."""
        config = BatteryConfig(
            capacity_kwh=10.0,
            max_charge_power_kw=5.0,
            max_discharge_power_kw=5.0,
            round_trip_efficiency=0.90,
            min_soc_percent=10.0,
            max_soc_percent=90.0,
            # Explicitly no derating (defaults)
        )
        prices = [0.05, 0.05, 0.30, 0.30]
        result = optimize_battery_schedule(
            battery_config=config,
            current_soc_kwh=5.0,
            price_forecast=prices,
            feed_in_forecast=None,
            pv_forecast=[0.0] * 4,
            consumption_forecast=[0.5] * 4,
            step_durations_hours=[0.25] * 4,
            degradation_cost_per_kwh=0.03,
            min_price_spread=0.05,
        )
        assert len(result.power_schedule_kw) == 4
        assert result.savings >= 0

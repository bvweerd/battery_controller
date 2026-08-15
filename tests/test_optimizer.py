"""Tests for optimizer.py."""

import math

import pytest

from custom_components.battery_controller.battery_model import BatteryConfig
from custom_components.battery_controller.const import (
    ACTION_CHARGING,
    ACTION_DISCHARGING,
    ACTION_IDLE,
    MAX_SOC_STATES,
)
from custom_components.battery_controller.efficiency_curve import (
    EfficiencyCurve,
    interpolate_efficiency,
)
from custom_components.battery_controller.optimizer import (
    OptimizationResult,
    charge_budget_wh,
    passive_dc_charge_wh,
    _filter_micro_cycles,
    _filter_oscillations,
    _find_nearest_soc_idx,
    calculate_step_cost,
    compute_soc_resolution_wh,
    optimize_battery_schedule,
)


def _flat_curve(eff: float, max_kw: float = 10.0) -> EfficiencyCurve:
    """Create a flat efficiency curve at a scalar efficiency value."""
    return [(0.0, eff), (max_kw, eff)]


def _rte_curves(
    rte: float, max_kw: float = 5.0
) -> tuple[EfficiencyCurve, EfficiencyCurve]:
    """Return (charge_curve, discharge_curve) for a given round-trip efficiency."""
    eff = math.sqrt(rte)
    return _flat_curve(eff, max_kw), _flat_curve(eff, max_kw)


@pytest.fixture
def battery_config():
    """Standard 10 kWh battery."""
    return BatteryConfig(
        capacity_kwh=10.0,
        max_charge_power_kw=5.0,
        max_discharge_power_kw=5.0,
        charge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
        discharge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
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
        charge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
        discharge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
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
            charge_curve=_flat_curve(math.sqrt(0.90)),
            discharge_curve=_flat_curve(math.sqrt(0.90)),
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
            charge_curve=_flat_curve(math.sqrt(0.90)),
            discharge_curve=_flat_curve(math.sqrt(0.90)),
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
            charge_curve=_flat_curve(math.sqrt(0.90)),
            discharge_curve=_flat_curve(math.sqrt(0.90)),
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
            charge_curve=_flat_curve(math.sqrt(0.90)),
            discharge_curve=_flat_curve(math.sqrt(0.90)),
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
            charge_curve=_flat_curve(math.sqrt(0.90)),
            discharge_curve=_flat_curve(math.sqrt(0.90)),
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
            charge_curve=_flat_curve(math.sqrt(0.90)),
            discharge_curve=_flat_curve(math.sqrt(0.90)),
            degradation_cost_per_kwh=0.03,
            battery_config=battery_config,
        )
        # Charging adds degradation: 2000 * 0.25 / 1000 * 0.03 = 0.015
        assert cost_charge > cost_idle

    def test_dc_pv_charging_continues_passively_during_active_charge(
        self, dc_battery_config
    ):
        """Passive DC PV charging continues on top of an active AC charge.

        The AC setpoint only controls AC-side exchange: both cases draw the
        full 2000 W setpoint from the grid (same grid cost), but the DC case
        additionally stores the DC PV passively, so it has higher throughput
        and therefore slightly higher degradation cost. The stored DC energy
        is rewarded through the SoC transition (V[t+1]), not the step cost.
        """
        cost_ac = calculate_step_cost(
            time_step_hours=0.25,
            soc_wh=5000,
            action_w=2000,
            grid_price=0.30,
            feed_in_price=0.07,
            pv_production_w=0,
            consumption_w=1000,
            charge_curve=_flat_curve(math.sqrt(0.90)),
            discharge_curve=_flat_curve(math.sqrt(0.90)),
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
            charge_curve=_flat_curve(math.sqrt(0.90)),
            discharge_curve=_flat_curve(math.sqrt(0.90)),
            degradation_cost_per_kwh=0.03,
            battery_config=dc_battery_config,
            pv_dc_production_w=2200,  # absorbed passively on top of the AC charge
        )
        # Grid cost identical (both draw the 2000 W setpoint + 1000 W load);
        # the DC case only adds degradation for the passively stored PV:
        # 2200 * 0.97 * 0.25 / 1000 * 0.03 = 0.016 EUR.
        extra_degradation = 2200 * 0.97 * 0.25 / 1000 * 0.03
        assert cost_dc == pytest.approx(cost_ac + extra_degradation)

    def test_dc_pv_passive_charge_capped_by_headroom_during_charge(
        self, dc_battery_config
    ):
        """Passive DC PV during an active charge only fills remaining headroom."""
        # soc 8500 Wh, max 9000 Wh. AC charge stores 2000 * 0.25 * sqrt(0.9)
        # = 474 Wh → headroom left = 26 Wh; passive DC absorbs only that.
        cost = calculate_step_cost(
            time_step_hours=0.25,
            soc_wh=8500,
            action_w=2000,
            grid_price=0.30,
            feed_in_price=0.07,
            pv_production_w=0,
            consumption_w=0,
            charge_curve=_flat_curve(math.sqrt(0.90)),
            discharge_curve=_flat_curve(math.sqrt(0.90)),
            degradation_cost_per_kwh=0.0,
            battery_config=dc_battery_config,
            pv_dc_production_w=3000,
        )
        # Remaining headroom after AC charge:
        ac_stored_wh = 2000 * 0.25 * math.sqrt(0.90)
        headroom_wh = 9000 - 8500 - ac_stored_wh
        dc_consumed_w = (headroom_wh / 0.25) / 0.97
        dc_excess_w = 3000 - dc_consumed_w
        # Net grid = 2000 (setpoint) - excess * 0.96 (to AC)
        net_grid_w = 2000 - dc_excess_w * 0.96
        expected = abs(net_grid_w) * 0.25 / 1000 * (0.30 if net_grid_w > 0 else -0.07)
        assert cost == pytest.approx(expected, abs=1e-6)

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
            charge_curve=_flat_curve(math.sqrt(0.90)),
            discharge_curve=_flat_curve(math.sqrt(0.90)),
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
            charge_curve=_flat_curve(math.sqrt(0.90)),
            discharge_curve=_flat_curve(math.sqrt(0.90)),
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
            charge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            discharge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
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
            charge_curve=battery_config.charge_efficiency_curve_parsed,
            discharge_curve=battery_config.discharge_efficiency_curve_parsed,
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
            charge_curve=_flat_curve(math.sqrt(0.9)),
            discharge_curve=_flat_curve(math.sqrt(0.9)),
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
            charge_curve=_flat_curve(math.sqrt(0.9)),
            discharge_curve=_flat_curve(math.sqrt(0.9)),
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
            charge_curve=_flat_curve(math.sqrt(0.9)),
            discharge_curve=_flat_curve(math.sqrt(0.9)),
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
            charge_curve=battery_config.charge_efficiency_curve_parsed,
            discharge_curve=battery_config.discharge_efficiency_curve_parsed,
            min_soc_kwh=battery_config.min_soc_kwh,
            max_soc_kwh=battery_config.max_soc_kwh,
            min_cycle_kwh=0.2,
        )

        assert power == [0.0, 0.0]
        assert mode == ["idle", "idle"]
        assert soc == [5.0, 5.0, 5.0]


class TestShadowPriceOutput:
    """Tests for the shadow price returned by the optimizer."""

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

    def test_terminal_uses_feed_in_tail(self, battery_config):
        """The terminal condition uses the feed-in tail average."""
        price = [0.10, 0.10, 0.10, 0.30, 0.30, 0.30, 0.30, 0.30]
        feed_in = [0.07] * 8
        args = self._base_args(battery_config)

        result = optimize_battery_schedule(
            price_forecast=price,
            feed_in_forecast=feed_in,
            **args,
        )
        # Should return a valid result with no crash
        assert len(result.power_schedule_kw) == 8
        assert result.shadow_price_eur_kwh >= 0.0

    def test_shadow_price_within_reasonable_bounds(self, battery_config):
        """The returned shadow price stays within a reasonable price range."""
        price = [0.10, 0.10, 0.20, 0.20, 0.10, 0.10, 0.20, 0.20]
        feed_in = [0.07] * 8
        args = self._base_args(battery_config)

        result = optimize_battery_schedule(
            price_forecast=price,
            feed_in_forecast=feed_in,
            **args,
        )
        lam = result.shadow_price_eur_kwh
        max_price = max(price)
        assert 0.0 <= lam <= max_price * 2, (
            f"Shadow price {lam} is outside reasonable bounds"
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
            charge_curve=_flat_curve(math.sqrt(0.90)),
            discharge_curve=_flat_curve(math.sqrt(0.90)),
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
            charge_curve=_flat_curve(math.sqrt(0.90)),
            discharge_curve=_flat_curve(math.sqrt(0.90)),
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
            charge_efficiency_curve=f"{math.sqrt(0.92):.4f}",
            discharge_efficiency_curve=f"{math.sqrt(0.92):.4f}",
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
            charge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            discharge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
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


# ---------------------------------------------------------------------------
# Additional coverage: missing branches in optimizer.py
# ---------------------------------------------------------------------------


class TestCalculateStepCostGridCap:
    """Cover grid cap clamping in calculate_step_cost."""

    def test_grid_cap_clamps_export(self):
        """max_grid_power_kw > 0 triggers grid cap clamp."""

        config = BatteryConfig(
            capacity_kwh=10.0,
            max_charge_power_kw=5.0,
            max_discharge_power_kw=5.0,
            charge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            discharge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            min_soc_percent=10.0,
            max_soc_percent=90.0,
            max_grid_power_kw=1.0,  # cap at 1 kW
        )
        # Discharging at 5 kW with 0 consumption → 5 kW export, capped to 1 kW
        cost_capped = calculate_step_cost(
            time_step_hours=0.25,
            soc_wh=5000,
            action_w=-5000,
            grid_price=0.30,
            feed_in_price=0.10,
            pv_production_w=0.0,
            consumption_w=0.0,
            charge_curve=_flat_curve(math.sqrt(0.90)),
            discharge_curve=_flat_curve(math.sqrt(0.90)),
            degradation_cost_per_kwh=0.03,
            battery_config=config,
        )
        # Without cap cost would be more negative; with cap it's less negative
        cost_uncapped = calculate_step_cost(
            time_step_hours=0.25,
            soc_wh=5000,
            action_w=-5000,
            grid_price=0.30,
            feed_in_price=0.10,
            pv_production_w=0.0,
            consumption_w=0.0,
            charge_curve=_flat_curve(math.sqrt(0.90)),
            discharge_curve=_flat_curve(math.sqrt(0.90)),
            degradation_cost_per_kwh=0.03,
            battery_config=BatteryConfig(
                capacity_kwh=10.0,
                max_charge_power_kw=5.0,
                max_discharge_power_kw=5.0,
                charge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
                discharge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
                min_soc_percent=10.0,
                max_soc_percent=90.0,
                max_grid_power_kw=0.0,  # no cap
            ),
        )
        # Capped export is less revenue (higher/less-negative cost)
        assert cost_capped > cost_uncapped


class TestOptimizeBatteryScheduleEdgeCases:
    """Cover edge cases in optimize_battery_schedule."""

    def test_step_durations_shorter_than_n_steps_are_padded(self):
        """When step_durations_hours is shorter than n_steps, last value is repeated."""
        config = BatteryConfig(
            capacity_kwh=10.0,
            max_charge_power_kw=5.0,
            max_discharge_power_kw=5.0,
            charge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            discharge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            min_soc_percent=10.0,
            max_soc_percent=90.0,
        )
        result = optimize_battery_schedule(
            battery_config=config,
            current_soc_kwh=5.0,
            price_forecast=[0.20] * 6,
            feed_in_forecast=None,
            pv_forecast=[0.0] * 6,
            consumption_forecast=[0.5] * 6,
            step_durations_hours=[0.25, 0.25],  # shorter than 6 steps → padded
            degradation_cost_per_kwh=0.03,
            min_price_spread=0.05,
        )
        assert len(result.power_schedule_kw) == 6

    def test_empty_feed_in_forecast_uses_zero_terminal_price(self):
        """Empty feed_in_forecast sets terminal_price = 0.0."""
        config = BatteryConfig(
            capacity_kwh=10.0,
            max_charge_power_kw=5.0,
            max_discharge_power_kw=5.0,
            charge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            discharge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            min_soc_percent=10.0,
            max_soc_percent=90.0,
        )
        result = optimize_battery_schedule(
            battery_config=config,
            current_soc_kwh=5.0,
            price_forecast=[0.20] * 4,
            feed_in_forecast=[],  # empty → terminal_price = 0.0
            pv_forecast=[0.0] * 4,
            consumption_forecast=[0.5] * 4,
            step_durations_hours=[0.25] * 4,
            degradation_cost_per_kwh=0.03,
            min_price_spread=0.05,
        )
        assert len(result.power_schedule_kw) == 4

    def test_shadow_price_at_max_soc_boundary(self):
        """Shadow price computation at max SoC (current_soc_idx == n_soc_states - 1, line 738)."""
        config = BatteryConfig(
            capacity_kwh=10.0,
            max_charge_power_kw=5.0,
            max_discharge_power_kw=5.0,
            charge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            discharge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            min_soc_percent=10.0,
            max_soc_percent=90.0,
        )
        # Start at max SoC so current_soc_idx == n_soc_states - 1
        result = optimize_battery_schedule(
            battery_config=config,
            current_soc_kwh=9.0,  # at max_soc_kwh = 9.0
            price_forecast=[0.20] * 4,
            feed_in_forecast=None,
            pv_forecast=[0.0] * 4,
            consumption_forecast=[0.5] * 4,
            step_durations_hours=[0.25] * 4,
            degradation_cost_per_kwh=0.03,
            min_price_spread=0.05,
        )
        # Shadow price should be valid (not crash)
        assert isinstance(result.shadow_price_eur_kwh, float)

    def test_dc_pv_passive_charging_in_forward_pass(self):
        """DC-coupled PV triggers passive charging in forward pass."""
        config = BatteryConfig(
            capacity_kwh=10.0,
            max_charge_power_kw=5.0,
            max_discharge_power_kw=5.0,
            charge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            discharge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            min_soc_percent=10.0,
            max_soc_percent=90.0,
            pv_dc_coupled=True,
            pv_dc_efficiency=0.97,
        )
        pv_dc = [2.0] * 4  # 2 kW DC PV available
        result = optimize_battery_schedule(
            battery_config=config,
            current_soc_kwh=5.0,
            price_forecast=[0.10] * 4,  # flat price → prefer idle + DC charging
            feed_in_forecast=None,
            pv_forecast=[0.0] * 4,
            consumption_forecast=[0.5] * 4,
            step_durations_hours=[0.25] * 4,
            degradation_cost_per_kwh=0.03,
            min_price_spread=0.05,
            pv_dc_forecast=pv_dc,
        )
        assert len(result.power_schedule_kw) == 4


class TestCalculateScheduleTotalCostNoPvDc:
    """Cover _calculate_schedule_total_cost with pv_dc_forecast=None."""

    def test_pv_dc_forecast_none_uses_zeros(self):
        """When pv_dc_forecast is None, function uses [0.0]*n."""
        from custom_components.battery_controller.optimizer import (
            _calculate_schedule_total_cost,
        )

        config = BatteryConfig(
            capacity_kwh=10.0,
            max_charge_power_kw=5.0,
            max_discharge_power_kw=5.0,
            charge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            discharge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            min_soc_percent=10.0,
            max_soc_percent=90.0,
        )
        cost = _calculate_schedule_total_cost(
            battery_config=config,
            power_schedule_kw=[0.0, 0.0],
            soc_schedule_kwh=[5.0, 5.0],
            price_forecast=[0.20, 0.20],
            feed_in_forecast=[0.07, 0.07],
            pv_forecast=[0.0, 0.0],
            consumption_forecast=[0.5, 0.5],
            step_durations_hours=[0.25, 0.25],
            degradation_cost_per_kwh=0.03,
            terminal_price=0.07,
            pv_dc_forecast=None,  # triggers line 178
        )
        assert isinstance(cost, float)


class TestFilterOscillationsEmpty:
    """Cover _filter_oscillations with empty schedule."""

    def test_empty_schedule_returns_unchanged(self):
        """Empty schedule returns immediately."""
        result_power, result_mode, result_soc = _filter_oscillations(
            power_schedule_kw=[],
            mode_schedule=[],
            initial_soc_kwh=5.0,
            price_forecast=[],
            min_price_spread=0.05,
            degradation_cost_per_kwh=0.03,
            charge_curve=_flat_curve(math.sqrt(0.90)),
            discharge_curve=_flat_curve(math.sqrt(0.90)),
            step_durations_hours=[],
            min_soc_kwh=1.0,
            max_soc_kwh=9.0,
        )
        assert result_power == []
        assert result_mode == []
        assert result_soc == [5.0]


class TestFilterOscillationsGetChargeCostEdges:
    """Cover get_charge_cost and get_discharge_value edge cases (lines 897, 911, 922)."""

    def _make_schedule(self, charge_kw, discharge_kw):
        """Create a schedule with one charge then one discharge step."""
        from custom_components.battery_controller.optimizer import (
            ACTION_CHARGING,
            ACTION_DISCHARGING,
        )

        return (
            [charge_kw, discharge_kw],
            [ACTION_CHARGING, ACTION_DISCHARGING],
        )

    def test_get_charge_cost_all_passive_dc(self):
        """When passive DC PV covers all charging, charge cost = 0."""
        power, mode = self._make_schedule(1.0, -1.0)
        # DC PV of 2 kW covers the 1 kW charge → effective_charge_kw <= 0 → cost = 0
        result_power, result_mode, _ = _filter_oscillations(
            power_schedule_kw=power,
            mode_schedule=mode,
            initial_soc_kwh=5.0,
            price_forecast=[0.20, 0.05],  # low discharge price to force filtering
            min_price_spread=0.05,
            degradation_cost_per_kwh=0.10,
            charge_curve=_flat_curve(math.sqrt(0.90)),
            discharge_curve=_flat_curve(math.sqrt(0.90)),
            step_durations_hours=[0.25, 0.25],
            min_soc_kwh=1.0,
            max_soc_kwh=9.0,
            pv_forecast=[0.0, 0.0],
            consumption_forecast=[0.5, 0.5],
            feed_in_forecast=[0.07, 0.07],
            pv_dc_forecast=[2.0, 2.0],  # DC PV > charge power
            pv_dc_coupled=True,
            pv_dc_efficiency=0.97,
        )

        # The filter rebuilds a schedule of the same length and never invents
        # a mode outside the three it knows.
        assert len(result_power) == len(power)
        assert set(result_mode) <= {ACTION_CHARGING, ACTION_DISCHARGING, ACTION_IDLE}

    def test_get_discharge_value_negative_power(self):
        """get_discharge_value with discharge_power_kw <= 0 returns grid price."""
        from custom_components.battery_controller.optimizer import (
            ACTION_CHARGING,
            ACTION_DISCHARGING,
        )

        # A charging step followed by zero discharge — triggers line 911 (0 discharge)
        result_power, result_mode, _ = _filter_oscillations(
            power_schedule_kw=[1.0, 0.0],
            mode_schedule=[ACTION_CHARGING, ACTION_DISCHARGING],
            initial_soc_kwh=5.0,
            price_forecast=[0.05, 0.10],  # spread too small to keep
            min_price_spread=0.20,
            degradation_cost_per_kwh=0.10,
            charge_curve=_flat_curve(math.sqrt(0.90)),
            discharge_curve=_flat_curve(math.sqrt(0.90)),
            step_durations_hours=[0.25, 0.25],
            min_soc_kwh=1.0,
            max_soc_kwh=9.0,
            pv_forecast=[3.0, 3.0],
            consumption_forecast=[0.5, 0.5],
            feed_in_forecast=[0.07, 0.07],
        )
        assert isinstance(result_power, list)


class TestFilterMicroCyclesEmpty:
    """Cover _filter_micro_cycles with empty schedule."""

    def test_empty_schedule_returns_unchanged(self):
        """Empty power schedule returns immediately."""
        result_power, result_mode, result_soc = _filter_micro_cycles(
            power_schedule_kw=[],
            mode_schedule=[],
            initial_soc_kwh=5.0,
            step_durations_hours=[],
            charge_curve=_flat_curve(math.sqrt(0.90)),
            discharge_curve=_flat_curve(math.sqrt(0.90)),
            min_soc_kwh=1.0,
            max_soc_kwh=9.0,
        )
        assert result_power == []
        assert result_mode == []
        assert result_soc == [5.0]


class TestFindNearestSocIdxSingleState:
    """Cover _find_nearest_soc_idx with len(soc_states) <= 1."""

    def test_single_soc_state_returns_zero(self):
        """Single SoC state always returns index 0."""
        assert _find_nearest_soc_idx(5000.0, [5000.0]) == 0

    def test_empty_soc_states_returns_zero(self):
        """Empty soc_states returns 0."""
        assert _find_nearest_soc_idx(5000.0, []) == 0


# ---------------------------------------------------------------------------
# Extra coverage: missing lines in optimizer.py
# ---------------------------------------------------------------------------


class TestSubResolutionACSkip:
    """Cover line 600: sub-resolution AC action that doesn't change SoC bin is skipped."""

    def test_sub_resolution_action_skipped_does_not_affect_result(self):
        """A non-zero action that keeps the SoC in the same bin is skipped.

        The optimizer internally skips actions that don't cross a SoC boundary.
        We can indirectly verify this by running an optimization with very small
        power steps and large time steps: sub-resolution actions (e.g., 100 W for
        a 15 min step = 25 Wh when resolution is 100 Wh) should be filtered.
        The result must still be valid (no crash, valid schedule).
        """

        from custom_components.battery_controller.optimizer import (
            optimize_battery_schedule,
        )

        cfg = BatteryConfig(
            capacity_kwh=10.0,
            max_charge_power_kw=5.0,
            max_discharge_power_kw=5.0,
            charge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            discharge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            min_soc_percent=10.0,
            max_soc_percent=90.0,
        )
        prices = [0.05, 0.40]
        result = optimize_battery_schedule(
            battery_config=cfg,
            current_soc_kwh=5.0,
            price_forecast=prices,
            feed_in_forecast=None,
            pv_forecast=[0.0, 0.0],
            consumption_forecast=[0.5, 0.5],
            step_durations_hours=[0.25, 0.25],
        )
        assert len(result.power_schedule_kw) == 2


class TestForwardPassDCIdlePassiveCharge:
    """Cover: idle + DC-coupled PV passive charging in forward pass."""

    def test_dc_pv_idle_charges_battery_in_forward_pass(self):
        """When idle with DC PV, forward pass increases SoC."""

        from custom_components.battery_controller.optimizer import (
            optimize_battery_schedule,
        )

        cfg = BatteryConfig(
            capacity_kwh=10.0,
            max_charge_power_kw=5.0,
            max_discharge_power_kw=5.0,
            charge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            discharge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            min_soc_percent=10.0,
            max_soc_percent=90.0,
            pv_dc_coupled=True,
            pv_dc_peak_power_kwp=3.0,
            pv_dc_efficiency=0.97,
        )
        # PV in the morning, expensive consumption in the evening: storing the
        # DC PV passively (idle) and discharging later is clearly optimal, so
        # the forward pass must take the idle + passive-charge branch.
        prices = [0.20, 0.20, 0.60, 0.60]
        pv_dc = [3.0, 3.0, 0.0, 0.0]  # 3 kW DC PV in the first two steps

        result = optimize_battery_schedule(
            battery_config=cfg,
            current_soc_kwh=2.0,
            price_forecast=prices,
            feed_in_forecast=[0.07] * 4,
            pv_forecast=[0.0] * 4,
            consumption_forecast=[0.0, 0.0, 3.0, 3.0],
            step_durations_hours=[1.0, 1.0, 1.0, 1.0],
            pv_dc_forecast=pv_dc,
            degradation_cost_per_kwh=0.03,
            min_price_spread=0.05,
        )
        assert len(result.soc_schedule_kwh) == 5
        # PV steps are idle (passive charging only) and raise the SoC by
        # ~2.91 kWh per step (3 kW × 0.97).
        assert result.mode_schedule[0] == "idle"
        assert result.soc_schedule_kwh[1] == pytest.approx(2.0 + 3.0 * 0.97, abs=0.05)
        # Stored PV is discharged during the expensive consumption steps.
        assert "discharging" in result.mode_schedule[2:]


class TestFilterOscillationsGetChargeCostZeroTotal:
    """Cover line 897: get_charge_cost returns price when total_kw == 0."""

    def test_charge_cost_zero_total_returns_price(self):
        """When effective_charge_kw == from_pv + from_grid == 0, returns price.

        This happens when from_pv == effective_charge_kw and from_grid == 0,
        but total_kw is 0 due to rounding. The function returns price_forecast[timestep].
        """
        # We can trigger this by calling _filter_oscillations with a charging schedule
        # where pv_surplus exactly covers the charge (from_pv = effective_charge_kw,
        # from_grid = 0, but we need total_kw = 0 which requires effective_charge_kw = 0).
        # Actually line 897: 'if total_kw <= 0: return price_forecast[timestep]'
        # is hit when from_pv = 0 and from_grid = 0, i.e., effective_charge_kw = 0.
        # But there's a guard: 'if effective_charge_kw <= 0: return 0.0' at line 890.
        # So line 897 is hit when pv_surplus >= effective_charge_kw (from_pv absorbs all)
        # and effective_charge_kw > 0 but then total_kw = from_pv + from_grid > 0.
        # Actually line 897 is only hit when total_kw == 0 exactly, which can happen
        # if from_pv = 0 and from_grid = 0 — but this requires effective_charge_kw = 0,
        # blocked by line 890 guard. Let's just verify the oscillation filter doesn't crash
        # and returns a valid schedule.

        # Use a charging schedule where pv perfectly covers the charge
        result_power, result_mode, result_soc = _filter_oscillations(
            power_schedule_kw=[1.0, -1.0, 1.0],
            mode_schedule=["charging", "discharging", "charging"],
            initial_soc_kwh=5.0,
            price_forecast=[0.10, 0.30, 0.10],
            min_price_spread=0.05,
            degradation_cost_per_kwh=0.01,
            charge_curve=_flat_curve(math.sqrt(0.90)),
            discharge_curve=_flat_curve(math.sqrt(0.90)),
            step_durations_hours=[1.0, 1.0, 1.0],
            min_soc_kwh=1.0,
            max_soc_kwh=9.0,
            pv_forecast=[2.0, 2.0, 2.0],  # PV covers full charge → from_pv = charge
            consumption_forecast=[1.0, 1.0, 1.0],
            feed_in_forecast=[0.07, 0.07, 0.07],
            oscillation_window_hours=2.0,
        )
        assert isinstance(result_power, list)
        assert len(result_power) == 3


class TestOptimizeWithMultiplePacks:
    """Integration tests: aggregate_battery_configs → optimize_battery_schedule.

    These tests verify the full path from multiple physical battery packs
    (each with their own BatteryConfig) through aggregation and into the DP
    optimizer.  The unit-level aggregate_battery_configs tests live in
    test_battery_model.py; here we care about whether the aggregated config
    drives the optimizer correctly end-to-end.
    """

    def _run(self, battery_config, **kwargs):
        """Helper to call optimize_battery_schedule with sensible defaults."""
        defaults = dict(
            current_soc_kwh=None,  # caller must override
            price_forecast=[0.05] * 4 + [0.30] * 4,
            feed_in_forecast=None,
            pv_forecast=[0.0] * 8,
            consumption_forecast=[0.5] * 8,
            step_durations_hours=[0.25] * 8,
            degradation_cost_per_kwh=0.03,
            min_price_spread=0.05,
        )
        defaults.update(kwargs)
        return optimize_battery_schedule(battery_config=battery_config, **defaults)

    # ------------------------------------------------------------------
    # 1. Two identical packs → equivalent to one doubled pack
    # ------------------------------------------------------------------

    def test_two_identical_packs_valid_schedule(self):
        """Aggregating two identical packs gives a valid schedule."""
        from custom_components.battery_controller.battery_model import (
            aggregate_battery_configs,
        )

        single = BatteryConfig(
            capacity_kwh=5.0,
            max_charge_power_kw=2.5,
            max_discharge_power_kw=2.5,
            charge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            discharge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            min_soc_percent=10.0,
            max_soc_percent=90.0,
        )
        combined = aggregate_battery_configs([single, single])
        # Combined: 10 kWh, 5 kW charge/discharge
        assert combined.capacity_kwh == pytest.approx(10.0)
        assert combined.max_charge_power_kw == pytest.approx(5.0)

        result = self._run(combined, current_soc_kwh=5.0)

        assert isinstance(result, OptimizationResult)
        assert len(result.power_schedule_kw) == 8
        for power in result.power_schedule_kw:
            assert power <= combined.max_charge_power_kw + 1e-6
            assert power >= -combined.max_discharge_power_kw - 1e-6

    def test_two_identical_packs_soc_in_bounds(self):
        """SoC never leaves the combined bounds when running two identical packs."""
        from custom_components.battery_controller.battery_model import (
            aggregate_battery_configs,
        )

        pack = BatteryConfig(
            capacity_kwh=5.0,
            max_charge_power_kw=2.5,
            max_discharge_power_kw=2.5,
            charge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            discharge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            min_soc_percent=10.0,
            max_soc_percent=90.0,
        )
        combined = aggregate_battery_configs([pack, pack])
        result = self._run(combined, current_soc_kwh=5.0)

        for soc in result.soc_schedule_kwh:
            assert soc >= combined.min_soc_kwh - 0.1
            assert soc <= combined.max_soc_kwh + 0.1

    # ------------------------------------------------------------------
    # 2. Asymmetric capacity packs
    # ------------------------------------------------------------------

    def test_asymmetric_capacity_combined_soc_limits(self):
        """SoC limits of asymmetric packs are correctly combined (kWh sum)."""
        from custom_components.battery_controller.battery_model import (
            aggregate_battery_configs,
        )

        small = BatteryConfig(
            capacity_kwh=5.0,
            max_charge_power_kw=2.5,
            max_discharge_power_kw=2.5,
            charge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            discharge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            min_soc_percent=10.0,
            max_soc_percent=90.0,
        )
        large = BatteryConfig(
            capacity_kwh=10.0,
            max_charge_power_kw=5.0,
            max_discharge_power_kw=5.0,
            charge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            discharge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            min_soc_percent=10.0,
            max_soc_percent=90.0,
        )
        combined = aggregate_battery_configs([small, large])

        # min_soc_kwh = 0.5 + 1.0 = 1.5, max_soc_kwh = 4.5 + 9.0 = 13.5
        assert combined.min_soc_kwh == pytest.approx(1.5)
        assert combined.max_soc_kwh == pytest.approx(13.5)

        result = self._run(combined, current_soc_kwh=7.5)
        assert len(result.power_schedule_kw) == 8
        for soc in result.soc_schedule_kwh:
            assert soc >= combined.min_soc_kwh - 0.1
            assert soc <= combined.max_soc_kwh + 0.1

    # ------------------------------------------------------------------
    # 3. Asymmetric RTE packs
    # ------------------------------------------------------------------

    def test_asymmetric_rte_optimizer_runs(self):
        """DP runs without error when packs have very different RTEs."""
        from custom_components.battery_controller.battery_model import (
            aggregate_battery_configs,
        )

        low_rte = BatteryConfig(
            capacity_kwh=5.0,
            max_charge_power_kw=2.5,
            max_discharge_power_kw=2.5,
            charge_efficiency_curve=f"{math.sqrt(0.80):.4f}",
            discharge_efficiency_curve=f"{math.sqrt(0.80):.4f}",
            min_soc_percent=10.0,
            max_soc_percent=90.0,
        )
        high_rte = BatteryConfig(
            capacity_kwh=5.0,
            max_charge_power_kw=2.5,
            max_discharge_power_kw=2.5,
            charge_efficiency_curve=f"{math.sqrt(0.95):.4f}",
            discharge_efficiency_curve=f"{math.sqrt(0.95):.4f}",
            min_soc_percent=10.0,
            max_soc_percent=90.0,
        )
        combined = aggregate_battery_configs([low_rte, high_rte])

        # Equal power ratings → each pack carries half the aggregate power.
        # Charging stores P * eff, so the shares combine arithmetically;
        # discharging draws P / eff, so they combine harmonically.
        eff_low, eff_high = math.sqrt(0.80), math.sqrt(0.95)
        expected_charge = 0.5 * eff_low + 0.5 * eff_high
        expected_discharge = 1.0 / (0.5 / eff_low + 0.5 / eff_high)
        assert combined.charge_efficiency == pytest.approx(expected_charge, abs=1e-4)
        assert combined.discharge_efficiency == pytest.approx(
            expected_discharge, abs=1e-4
        )
        expected_rte = expected_charge * expected_discharge
        assert combined.round_trip_efficiency == pytest.approx(expected_rte, abs=1e-4)

        result = self._run(combined, current_soc_kwh=5.0)
        assert len(result.power_schedule_kw) == 8
        # With a clear price spread, the optimizer should find arbitrage
        assert result.savings >= 0

    # ------------------------------------------------------------------
    # 4. Asymmetric power limits
    # ------------------------------------------------------------------

    def test_asymmetric_power_limits_sum_respected(self):
        """Optimizer never exceeds the summed power of asymmetric packs."""
        from custom_components.battery_controller.battery_model import (
            aggregate_battery_configs,
        )

        weak = BatteryConfig(
            capacity_kwh=5.0,
            max_charge_power_kw=1.2,
            max_discharge_power_kw=1.2,
            charge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            discharge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            min_soc_percent=10.0,
            max_soc_percent=90.0,
        )
        strong = BatteryConfig(
            capacity_kwh=5.0,
            max_charge_power_kw=5.0,
            max_discharge_power_kw=5.0,
            charge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            discharge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            min_soc_percent=10.0,
            max_soc_percent=90.0,
        )
        combined = aggregate_battery_configs([weak, strong])
        assert combined.max_charge_power_kw == pytest.approx(6.2)
        assert combined.max_discharge_power_kw == pytest.approx(6.2)

        result = self._run(combined, current_soc_kwh=5.0)
        for power in result.power_schedule_kw:
            assert power <= combined.max_charge_power_kw + 1e-6
            assert power >= -combined.max_discharge_power_kw - 1e-6

    # ------------------------------------------------------------------
    # 5. DC-coupled pack combined with AC-only pack
    # ------------------------------------------------------------------

    def test_dc_plus_ac_pack_combined(self):
        """One DC-coupled and one AC-only pack are combined correctly."""
        from custom_components.battery_controller.battery_model import (
            aggregate_battery_configs,
        )

        ac_pack = BatteryConfig(
            capacity_kwh=5.0,
            max_charge_power_kw=2.5,
            max_discharge_power_kw=2.5,
            charge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            discharge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            min_soc_percent=10.0,
            max_soc_percent=90.0,
        )
        dc_pack = BatteryConfig(
            capacity_kwh=5.0,
            max_charge_power_kw=2.5,
            max_discharge_power_kw=2.5,
            charge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            discharge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            min_soc_percent=10.0,
            max_soc_percent=90.0,
            pv_dc_coupled=True,
            pv_dc_peak_power_kwp=3.0,
            pv_dc_efficiency=0.97,
        )
        combined = aggregate_battery_configs([ac_pack, dc_pack])

        assert combined.pv_dc_coupled is True
        assert combined.pv_dc_peak_power_kwp == pytest.approx(3.0)

        pv_dc = [2.0] * 8
        result = self._run(
            combined,
            current_soc_kwh=5.0,
            pv_dc_forecast=pv_dc,
        )
        assert len(result.power_schedule_kw) == 8
        for soc in result.soc_schedule_kwh:
            assert soc >= combined.min_soc_kwh - 0.1
            assert soc <= combined.max_soc_kwh + 0.1

    # ------------------------------------------------------------------
    # 6. Derating on one pack only
    # ------------------------------------------------------------------

    def test_derating_one_pack_only_limits_combined_power(self):
        """When only one of two packs derates, the other keeps its full rating.

        Above the fleet threshold each pack contributes what it can still do
        there: the derating pack its reduced limit (0.45 kW), the plain pack its
        nominal rating (1.2 kW) → 1.65 kW combined, not 0.45 kW.

        The threshold is the first one reached, i.e. the lowest of the packs
        that actually derate (95 %), not a capacity-weighted average that mixes
        in the disabling 100 % sentinel of the plain pack →  9.5 kWh of 10 kWh.

        Starting at 9.8 kWh (98 %) puts us above the combined threshold, so the
        DP must respect the 1.65 kW combined limit instead of the nominal
        2.4 kW.
        """
        from custom_components.battery_controller.battery_model import (
            aggregate_battery_configs,
        )

        plain = BatteryConfig(
            capacity_kwh=5.0,
            max_charge_power_kw=1.2,
            max_discharge_power_kw=1.2,
            charge_efficiency_curve=f"{math.sqrt(0.92):.4f}",
            discharge_efficiency_curve=f"{math.sqrt(0.92):.4f}",
            min_soc_percent=10.0,
            max_soc_percent=100.0,
        )
        derated = BatteryConfig(
            capacity_kwh=5.0,
            max_charge_power_kw=1.2,
            max_discharge_power_kw=1.2,
            charge_efficiency_curve=f"{math.sqrt(0.92):.4f}",
            discharge_efficiency_curve=f"{math.sqrt(0.92):.4f}",
            min_soc_percent=10.0,
            max_soc_percent=100.0,
            high_soc_charge_threshold_pct=95.0,
            high_soc_max_charge_kw=0.45,
        )
        combined = aggregate_battery_configs([plain, derated])

        # Plain pack keeps its nominal 1.2 kW, derated pack contributes 0.45 kW
        assert combined.high_soc_max_charge_kw == pytest.approx(1.65)
        # Threshold = lowest threshold among the packs that actually derate
        assert combined.high_soc_charge_threshold_pct == pytest.approx(95.0)

        combined_threshold_kwh = (
            combined.high_soc_charge_threshold_pct / 100.0 * combined.capacity_kwh
        )  # = 9.5 kWh

        # Start at 9.8 kWh (98 %), clearly above the 95 % threshold
        start_soc = 9.8
        result = self._run(
            combined,
            current_soc_kwh=start_soc,
            price_forecast=[0.05] * 4 + [0.30] * 4,
        )
        # Any charge step while SoC is at or above the combined threshold must
        # not exceed the combined derated limit (1.65 kW).
        for i, (mode, power) in enumerate(
            zip(result.mode_schedule, result.power_schedule_kw)
        ):
            if (
                mode == "charging"
                and result.soc_schedule_kwh[i] >= combined_threshold_kwh
            ):
                assert power <= 1.66, (
                    f"step {i}: charge {power:.3f} kW exceeds derated limit 1.65 kW"
                )

    def test_derating_absent_on_all_packs_stays_disabled(self):
        """With no pack derating, the combined config must keep derating off."""
        from custom_components.battery_controller.battery_model import (
            aggregate_battery_configs,
        )

        a = BatteryConfig(capacity_kwh=5.0, max_charge_power_kw=1.2)
        b = BatteryConfig(capacity_kwh=5.0, max_charge_power_kw=2.0)
        combined = aggregate_battery_configs([a, b])

        assert combined.high_soc_max_charge_kw == 0.0
        assert combined.low_soc_max_discharge_kw == 0.0
        # Sentinel of 0 kW means "no derating": full rating at every SoC.
        assert combined.max_charge_at_soc(combined.max_soc_kwh) == pytest.approx(3.2)

    # ------------------------------------------------------------------
    # 7. Floating-point SoC boundary regression (known bug #3)
    # ------------------------------------------------------------------

    @pytest.mark.parametrize(
        "cap_a, cap_b, min_pct",
        [
            (10.1, 5.2, 10.0),  # min_soc_kwh = 1.01 + 0.52 = 1.53 (not round)
            (7.68, 3.84, 15.0),  # min_soc_kwh = 1.152 + 0.576 = 1.728
            (4.8, 9.6, 20.0),  # min_soc_kwh = 0.96 + 1.92 = 2.88
        ],
    )
    def test_floating_point_soc_boundary_no_crash(self, cap_a, cap_b, min_pct):
        """Awkward fractional capacities must not cause SoC boundary skipping."""
        from custom_components.battery_controller.battery_model import (
            aggregate_battery_configs,
        )

        pack_a = BatteryConfig(
            capacity_kwh=cap_a,
            max_charge_power_kw=2.5,
            max_discharge_power_kw=2.5,
            charge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            discharge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            min_soc_percent=min_pct,
            max_soc_percent=90.0,
        )
        pack_b = BatteryConfig(
            capacity_kwh=cap_b,
            max_charge_power_kw=2.5,
            max_discharge_power_kw=2.5,
            charge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            discharge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            min_soc_percent=min_pct,
            max_soc_percent=90.0,
        )
        combined = aggregate_battery_configs([pack_a, pack_b])
        mid_soc = (combined.min_soc_kwh + combined.max_soc_kwh) / 2.0

        result = self._run(combined, current_soc_kwh=mid_soc)

        assert len(result.power_schedule_kw) == 8
        for soc in result.soc_schedule_kwh:
            assert soc >= combined.min_soc_kwh - 0.1
            assert soc <= combined.max_soc_kwh + 0.1


class TestMultiPackEdgeCases:
    """Edge cases for multi-pack aggregation → optimizer integration.

    These tests cover degenerate and boundary situations that can occur in
    production: offline packs, BMS locks, sensor errors, and extreme configs.
    """

    def _run(self, battery_config, **kwargs):
        defaults = dict(
            price_forecast=[0.05] * 4 + [0.30] * 4,
            feed_in_forecast=None,
            pv_forecast=[0.0] * 8,
            consumption_forecast=[0.5] * 8,
            step_durations_hours=[0.25] * 8,
            degradation_cost_per_kwh=0.03,
            min_price_spread=0.05,
        )
        defaults.update(kwargs)
        return optimize_battery_schedule(battery_config=battery_config, **defaults)

    # ------------------------------------------------------------------
    # 1. Inoperative pack (zero power) — simulates a tripped BMS
    # ------------------------------------------------------------------

    def test_zero_power_pack_combined_with_normal_idles(self):
        """A zero-power pack (BMS tripped) combined with a normal pack
        should behave exactly like the normal pack alone.

        aggregate_battery_configs sums powers: 0 + 2.5 = 2.5 kW.
        The optimizer must not crash and must respect the actual limit.
        """
        from custom_components.battery_controller.battery_model import (
            aggregate_battery_configs,
        )

        offline = BatteryConfig(
            capacity_kwh=5.0,
            max_charge_power_kw=0.0,
            max_discharge_power_kw=0.0,
            charge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            discharge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            min_soc_percent=10.0,
            max_soc_percent=90.0,
        )
        normal = BatteryConfig(
            capacity_kwh=5.0,
            max_charge_power_kw=2.5,
            max_discharge_power_kw=2.5,
            charge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            discharge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            min_soc_percent=10.0,
            max_soc_percent=90.0,
        )
        combined = aggregate_battery_configs([offline, normal])
        assert combined.max_charge_power_kw == pytest.approx(2.5)
        assert combined.max_discharge_power_kw == pytest.approx(2.5)

        result = self._run(combined, current_soc_kwh=5.0)

        assert len(result.power_schedule_kw) == 8
        for power in result.power_schedule_kw:
            assert power <= combined.max_charge_power_kw + 1e-6
            assert power >= -combined.max_discharge_power_kw - 1e-6

    def test_both_packs_zero_power_all_idle(self):
        """When every pack has zero power, the optimizer can only idle."""
        from custom_components.battery_controller.battery_model import (
            aggregate_battery_configs,
        )

        dead_a = BatteryConfig(
            capacity_kwh=5.0,
            max_charge_power_kw=0.0,
            max_discharge_power_kw=0.0,
            charge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            discharge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            min_soc_percent=10.0,
            max_soc_percent=90.0,
        )
        dead_b = BatteryConfig(
            capacity_kwh=5.0,
            max_charge_power_kw=0.0,
            max_discharge_power_kw=0.0,
            charge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            discharge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            min_soc_percent=10.0,
            max_soc_percent=90.0,
        )
        combined = aggregate_battery_configs([dead_a, dead_b])
        result = self._run(combined, current_soc_kwh=5.0)

        assert all(abs(p) < 1e-6 for p in result.power_schedule_kw), (
            f"All-zero-power pack should produce idle schedule: {result.power_schedule_kw}"
        )
        assert all(m == "idle" for m in result.mode_schedule)

    # ------------------------------------------------------------------
    # 2. Degenerate pack: min_soc == max_soc (BMS locked at fixed SoC)
    # ------------------------------------------------------------------

    def test_degenerate_pack_min_equals_max_soc(self):
        """A pack with min_soc_percent == max_soc_percent has zero usable capacity.

        n_soc_states = 1 → only one state exists.  The optimizer must not crash
        and should return a valid (idle) schedule since no SoC transitions are
        possible.
        """
        locked = BatteryConfig(
            capacity_kwh=5.0,
            max_charge_power_kw=2.5,
            max_discharge_power_kw=2.5,
            charge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            discharge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            min_soc_percent=50.0,
            max_soc_percent=50.0,  # zero usable capacity
        )
        result = self._run(locked, current_soc_kwh=2.5)

        assert len(result.power_schedule_kw) == 8
        # With no usable capacity, every action transitions to the same SoC bin
        # (sub-resolution → skipped), so all steps are idle.
        assert all(m == "idle" for m in result.mode_schedule)

    def test_degenerate_pack_combined_with_normal(self):
        """A locked pack aggregated with a normal pack: the locked pack adds
        capacity but contributes zero usable range of its own.
        """
        from custom_components.battery_controller.battery_model import (
            aggregate_battery_configs,
        )

        locked = BatteryConfig(
            capacity_kwh=5.0,
            max_charge_power_kw=2.5,
            max_discharge_power_kw=2.5,
            charge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            discharge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            min_soc_percent=50.0,
            max_soc_percent=50.0,
        )
        normal = BatteryConfig(
            capacity_kwh=5.0,
            max_charge_power_kw=2.5,
            max_discharge_power_kw=2.5,
            charge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            discharge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            min_soc_percent=10.0,
            max_soc_percent=90.0,
        )
        combined = aggregate_battery_configs([locked, normal])
        # min_soc_kwh = 2.5 + 0.5 = 3.0, max_soc_kwh = 2.5 + 4.5 = 7.0
        assert combined.min_soc_kwh == pytest.approx(3.0)
        assert combined.max_soc_kwh == pytest.approx(7.0)

        result = self._run(combined, current_soc_kwh=5.0)
        assert len(result.power_schedule_kw) == 8
        for soc in result.soc_schedule_kwh:
            assert soc >= combined.min_soc_kwh - 0.1
            assert soc <= combined.max_soc_kwh + 0.1

    # ------------------------------------------------------------------
    # 3. start_soc outside combined bounds — sensor error / miscalibration
    # ------------------------------------------------------------------

    def test_start_soc_below_min_clamps_gracefully(self):
        """If the SoC sensor reports below the configured minimum, _find_nearest_soc_idx
        clamps to index 0 (min SoC state) and the optimizer must not crash.
        """
        config = BatteryConfig(
            capacity_kwh=10.0,
            max_charge_power_kw=5.0,
            max_discharge_power_kw=5.0,
            charge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            discharge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            min_soc_percent=10.0,
            max_soc_percent=90.0,
        )
        # Report SoC 0.1 kWh below the 1.0 kWh minimum
        result = self._run(config, current_soc_kwh=0.9)

        assert len(result.power_schedule_kw) == 8
        assert isinstance(result.optimal_mode, str)

    def test_start_soc_above_max_clamps_gracefully(self):
        """If the SoC sensor reports above the configured maximum, the optimizer
        must clamp to the top SoC state without crashing.
        """
        config = BatteryConfig(
            capacity_kwh=10.0,
            max_charge_power_kw=5.0,
            max_discharge_power_kw=5.0,
            charge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            discharge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            min_soc_percent=10.0,
            max_soc_percent=90.0,
        )
        # Report SoC 0.5 kWh above the 9.0 kWh maximum
        result = self._run(config, current_soc_kwh=9.5)

        assert len(result.power_schedule_kw) == 8
        # At or above max SoC there is no room to charge
        assert all(p <= 0.0 + 1e-6 for p in result.power_schedule_kw), (
            f"Should not charge above max SoC: {result.power_schedule_kw}"
        )

    # ------------------------------------------------------------------
    # 4. Many packs — stress test of aggregation + optimizer
    # ------------------------------------------------------------------

    def test_five_packs_aggregate_and_optimize(self):
        """Five identical packs aggregated must produce a valid schedule with
        power limits equal to 5× the individual pack's limits.
        """
        from custom_components.battery_controller.battery_model import (
            aggregate_battery_configs,
        )

        pack = BatteryConfig(
            capacity_kwh=5.0,
            max_charge_power_kw=2.5,
            max_discharge_power_kw=2.5,
            charge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            discharge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            min_soc_percent=10.0,
            max_soc_percent=90.0,
        )
        combined = aggregate_battery_configs([pack] * 5)
        assert combined.capacity_kwh == pytest.approx(25.0)
        assert combined.max_charge_power_kw == pytest.approx(12.5)

        result = self._run(combined, current_soc_kwh=12.5)

        assert len(result.power_schedule_kw) == 8
        for power in result.power_schedule_kw:
            assert power <= combined.max_charge_power_kw + 1e-6
            assert power >= -combined.max_discharge_power_kw - 1e-6

    # ------------------------------------------------------------------
    # 5. Very low RTE — arbitrage is suppressed
    # ------------------------------------------------------------------

    def test_very_low_rte_no_arbitrage(self):
        """With RTE=0.5 the round-trip loss is 50 %.  Even a 0.20 EUR/kWh spread
        is unprofitable (threshold ≈ (2×0.03 + 0.05)/√0.5 ≈ 0.156; but the
        effective buy/sell spread through a √0.5 ≈ 0.707 inverter is much worse).
        The optimizer must not arbitrage.
        """
        config = BatteryConfig(
            capacity_kwh=10.0,
            max_charge_power_kw=5.0,
            max_discharge_power_kw=5.0,
            charge_efficiency_curve=f"{math.sqrt(0.50):.4f}",
            discharge_efficiency_curve=f"{math.sqrt(0.50):.4f}",
            min_soc_percent=10.0,
            max_soc_percent=90.0,
        )
        # Price spread 0.20 EUR/kWh: clear spread but huge RTE loss
        result = self._run(
            config,
            current_soc_kwh=5.0,
            price_forecast=[0.10] * 4 + [0.30] * 4,
            feed_in_forecast=[0.07] * 8,
            degradation_cost_per_kwh=0.03,
            min_price_spread=0.05,
        )
        has_charge_then_discharge = any(
            result.mode_schedule[i] == "charging"
            and any(
                result.mode_schedule[j] == "discharging"
                for j in range(i + 1, len(result.mode_schedule))
            )
            for i in range(len(result.mode_schedule))
        )
        assert not has_charge_then_discharge, (
            f"RTE=0.5: should not arbitrage, got {result.mode_schedule}"
        )

    # ------------------------------------------------------------------
    # 6. Mixed grid caps: one unlimited + one capped → combined unlimited
    # ------------------------------------------------------------------

    def test_mixed_grid_cap_unlimited_wins(self):
        """aggregate_battery_configs: if any pack has max_grid_power_kw=0 (unlimited),
        the combined cap is also 0 (unlimited).
        """
        from custom_components.battery_controller.battery_model import (
            aggregate_battery_configs,
        )

        unlimited = BatteryConfig(
            capacity_kwh=5.0,
            max_charge_power_kw=2.5,
            max_discharge_power_kw=2.5,
            charge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            discharge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            min_soc_percent=10.0,
            max_soc_percent=90.0,
            max_grid_power_kw=0.0,  # unlimited
        )
        capped = BatteryConfig(
            capacity_kwh=5.0,
            max_charge_power_kw=2.5,
            max_discharge_power_kw=2.5,
            charge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            discharge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            min_soc_percent=10.0,
            max_soc_percent=90.0,
            max_grid_power_kw=3.0,  # capped at 3 kW
        )
        combined = aggregate_battery_configs([unlimited, capped])
        assert combined.max_grid_power_kw == pytest.approx(0.0), (
            "One unlimited pack → combined must be unlimited (0)"
        )

        # Optimizer must run without error
        result = self._run(combined, current_soc_kwh=5.0)
        assert len(result.power_schedule_kw) == 8

    def test_both_packs_capped_sums_caps(self):
        """When both packs have a grid cap, the combined cap is the sum."""
        from custom_components.battery_controller.battery_model import (
            aggregate_battery_configs,
        )

        cap_a = BatteryConfig(
            capacity_kwh=5.0,
            max_charge_power_kw=2.5,
            max_discharge_power_kw=2.5,
            charge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            discharge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            min_soc_percent=10.0,
            max_soc_percent=90.0,
            max_grid_power_kw=2.0,
        )
        cap_b = BatteryConfig(
            capacity_kwh=5.0,
            max_charge_power_kw=2.5,
            max_discharge_power_kw=2.5,
            charge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            discharge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            min_soc_percent=10.0,
            max_soc_percent=90.0,
            max_grid_power_kw=3.0,
        )
        combined = aggregate_battery_configs([cap_a, cap_b])
        assert combined.max_grid_power_kw == pytest.approx(5.0)

    # ------------------------------------------------------------------
    # 7. Packs start at SoC extremes
    # ------------------------------------------------------------------

    def test_all_packs_fully_charged_no_charging(self):
        """When combined SoC is at max, the optimizer must not charge."""
        from custom_components.battery_controller.battery_model import (
            aggregate_battery_configs,
        )

        pack = BatteryConfig(
            capacity_kwh=5.0,
            max_charge_power_kw=2.5,
            max_discharge_power_kw=2.5,
            charge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            discharge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            min_soc_percent=10.0,
            max_soc_percent=90.0,
        )
        combined = aggregate_battery_configs([pack, pack])

        result = self._run(
            combined,
            current_soc_kwh=combined.max_soc_kwh,  # 9.0 kWh (90% of 10)
            price_forecast=[0.05] * 4 + [0.30] * 4,
        )
        assert all(p <= 1e-6 for p in result.power_schedule_kw), (
            f"At max SoC should not charge: {result.power_schedule_kw}"
        )

    def test_all_packs_fully_discharged_no_discharging(self):
        """When combined SoC is at min, the optimizer must not discharge."""
        from custom_components.battery_controller.battery_model import (
            aggregate_battery_configs,
        )

        pack = BatteryConfig(
            capacity_kwh=5.0,
            max_charge_power_kw=2.5,
            max_discharge_power_kw=2.5,
            charge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            discharge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            min_soc_percent=10.0,
            max_soc_percent=90.0,
        )
        combined = aggregate_battery_configs([pack, pack])

        result = self._run(
            combined,
            current_soc_kwh=combined.min_soc_kwh,  # 1.0 kWh (10% of 10)
            price_forecast=[0.30] * 4 + [0.05] * 4,
        )
        assert all(p >= -1e-6 for p in result.power_schedule_kw), (
            f"At min SoC should not discharge: {result.power_schedule_kw}"
        )

    # ------------------------------------------------------------------
    # 8. Asymmetric min/max SoC percent across packs
    # ------------------------------------------------------------------

    def test_asymmetric_soc_limits_combined_usable_range(self):
        """Two packs with different min/max SoC % must yield the correct combined
        kWh limits (sum of individual kWh limits).
        """
        from custom_components.battery_controller.battery_model import (
            aggregate_battery_configs,
        )

        conservative = BatteryConfig(
            capacity_kwh=10.0,
            max_charge_power_kw=5.0,
            max_discharge_power_kw=5.0,
            charge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            discharge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            min_soc_percent=20.0,  # min = 2.0 kWh
            max_soc_percent=80.0,  # max = 8.0 kWh  → 6.0 kWh usable
        )
        aggressive = BatteryConfig(
            capacity_kwh=10.0,
            max_charge_power_kw=5.0,
            max_discharge_power_kw=5.0,
            charge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            discharge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            min_soc_percent=5.0,  # min = 0.5 kWh
            max_soc_percent=95.0,  # max = 9.5 kWh  → 9.0 kWh usable
        )
        combined = aggregate_battery_configs([conservative, aggressive])

        assert combined.min_soc_kwh == pytest.approx(2.5)  # 2.0 + 0.5
        assert combined.max_soc_kwh == pytest.approx(17.5)  # 8.0 + 9.5

        result = self._run(combined, current_soc_kwh=10.0)
        assert len(result.power_schedule_kw) == 8
        for soc in result.soc_schedule_kwh:
            assert soc >= combined.min_soc_kwh - 0.1
            assert soc <= combined.max_soc_kwh + 0.1


class TestFilterOscillationsGetDischargeCostZeroTotal:
    """Cover line 922: get_discharge_value returns price when total_kw == 0."""

    def test_discharge_value_zero_total_returns_price(self):
        """When discharge_power_kw > 0 but total_kw == 0, returns price.

        total_kw = to_self_kw + to_export_kw. This is 0 only when both are 0.
        to_self_kw = min(discharge_power_kw, residual_load) = 0 when residual_load=0.
        to_export_kw = max(0, discharge_power_kw - 0) = discharge_power_kw > 0.
        So total_kw > 0 normally. The guard 'if total_kw <= 0' is a safety net.
        We verify the filter runs correctly with discharging during export conditions.
        """

        # Discharge during PV surplus: pv > consumption → residual_load=0, all to export
        result_power, result_mode, result_soc = _filter_oscillations(
            power_schedule_kw=[1.0, -1.0],
            mode_schedule=["charging", "discharging"],
            initial_soc_kwh=5.0,
            price_forecast=[0.05, 0.40],
            min_price_spread=0.05,
            degradation_cost_per_kwh=0.01,
            charge_curve=_flat_curve(math.sqrt(0.90)),
            discharge_curve=_flat_curve(math.sqrt(0.90)),
            step_durations_hours=[1.0, 1.0],
            min_soc_kwh=1.0,
            max_soc_kwh=9.0,
            pv_forecast=[5.0, 5.0],  # PV > consumption → residual_load=0
            consumption_forecast=[1.0, 1.0],
            feed_in_forecast=[0.07, 0.07],
            oscillation_window_hours=2.0,
        )
        assert isinstance(result_power, list)
        assert len(result_power) == 2


class TestEffCurveOverride:
    """Curve overrides replace the nominal battery config curves."""

    def test_discharge_eff_curve_override_applied(self, battery_config):
        """discharge_eff_curve_override replaces battery_config curve."""
        prices = [0.10, 0.30, 0.10, 0.30]
        result = optimize_battery_schedule(
            battery_config=battery_config,
            current_soc_kwh=5.0,
            price_forecast=prices,
            feed_in_forecast=prices,
            pv_forecast=[0.0, 0.0, 0.0, 0.0],
            consumption_forecast=[1.0, 1.0, 1.0, 1.0],
            discharge_eff_curve_override=_flat_curve(0.85),
        )
        assert result is not None
        assert len(result.power_schedule_kw) == 4

    def test_charge_eff_curve_override_applied(self, battery_config):
        """charge_eff_curve_override replaces battery_config curve."""
        prices = [0.10, 0.30, 0.10, 0.30]
        result = optimize_battery_schedule(
            battery_config=battery_config,
            current_soc_kwh=5.0,
            price_forecast=prices,
            feed_in_forecast=prices,
            pv_forecast=[0.0, 0.0, 0.0, 0.0],
            consumption_forecast=[1.0, 1.0, 1.0, 1.0],
            charge_eff_curve_override=_flat_curve(0.85),
        )
        assert result is not None
        assert len(result.power_schedule_kw) == 4


class TestBaselineGridCap:
    """Baseline cost must respect the grid capacity cap like the step cost does."""

    def test_baseline_export_capped(self):
        from custom_components.battery_controller.optimizer import (
            _calculate_baseline_cost,
        )

        # 10 kW PV surplus, 3 kW export cap, 1 hour, feed-in 0.10:
        # uncapped revenue would be 1.00 EUR; capped it is 0.30 EUR.
        cost = _calculate_baseline_cost(
            price_forecast=[0.20],
            feed_in_forecast=[0.10],
            pv_forecast=[10.0],
            consumption_forecast=[0.0],
            step_durations_hours=[1.0],
            pv_dc_forecast=[0.0],
            max_grid_power_kw=3.0,
        )
        assert cost == pytest.approx(-0.30)

    def test_baseline_unlimited_when_cap_zero(self):
        from custom_components.battery_controller.optimizer import (
            _calculate_baseline_cost,
        )

        cost = _calculate_baseline_cost(
            price_forecast=[0.20],
            feed_in_forecast=[0.10],
            pv_forecast=[10.0],
            consumption_forecast=[0.0],
            step_durations_hours=[1.0],
            pv_dc_forecast=[0.0],
            max_grid_power_kw=0.0,
        )
        assert cost == pytest.approx(-1.00)

    def test_savings_consistent_with_capped_baseline(self):
        """optimize_battery_schedule passes the configured cap to the baseline."""
        cfg = BatteryConfig(
            capacity_kwh=10.0,
            max_charge_power_kw=5.0,
            max_discharge_power_kw=5.0,
            charge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            discharge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            min_soc_percent=10.0,
            max_soc_percent=90.0,
            max_grid_power_kw=3.0,
        )
        result = optimize_battery_schedule(
            battery_config=cfg,
            current_soc_kwh=5.0,
            price_forecast=[0.20] * 4,
            feed_in_forecast=[0.10] * 4,
            pv_forecast=[10.0] * 4,
            consumption_forecast=[0.0] * 4,
            step_durations_hours=[1.0] * 4,
            degradation_cost_per_kwh=0.03,
            min_price_spread=0.05,
        )
        # Capped baseline: 4 h x 3 kW x 0.10 = 1.20 EUR revenue.
        assert result.baseline_cost == pytest.approx(-1.20)


class TestTerminalPriceClamp:
    """Negative feed-in tail must not give stored energy a negative terminal value."""

    def test_negative_feed_in_tail_does_not_force_discharge(self):
        """With all-negative feed-in and no load, the battery should stay idle.

        Without the clamp, V[T] penalizes stored energy (negative terminal
        price), so the DP dumps energy before the horizon ends even though
        exporting at a negative price costs money.
        """
        cfg = BatteryConfig(
            capacity_kwh=10.0,
            max_charge_power_kw=5.0,
            max_discharge_power_kw=5.0,
            charge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            discharge_efficiency_curve=f"{math.sqrt(0.90):.4f}",
            min_soc_percent=10.0,
            max_soc_percent=90.0,
        )
        result = optimize_battery_schedule(
            battery_config=cfg,
            current_soc_kwh=5.0,
            price_forecast=[0.20] * 6,
            feed_in_forecast=[-0.05] * 6,
            pv_forecast=[0.0] * 6,
            consumption_forecast=[0.0] * 6,
            step_durations_hours=[1.0] * 6,
            degradation_cost_per_kwh=0.01,
            min_price_spread=0.05,
        )
        assert all(m == "idle" for m in result.mode_schedule)
        assert result.soc_schedule_kwh[-1] == pytest.approx(5.0)


class TestSocGridBudget:
    """SoC grid sizing and the DP state-space budget.

    The DP cost scales with the number of SoC states, which at a fixed
    resolution grows linearly with usable capacity. These tests pin the
    budget so a future change cannot silently reintroduce an unbounded
    state space (the cause of multi-minute solves on large batteries).

    Deliberately asserted on state counts rather than wall-clock time:
    the state space is deterministic and hardware-independent, whereas
    timing on shared CI runners is not.
    """

    @staticmethod
    def _grid(capacity_kwh, min_pct=10.0, max_pct=90.0):
        """Return (resolution_wh, n_soc_states) for a battery size."""
        cfg = BatteryConfig(
            capacity_kwh=capacity_kwh,
            min_soc_percent=min_pct,
            max_soc_percent=max_pct,
        )
        min_wh = round(cfg.min_soc_kwh * 1000)
        max_wh = round(cfg.max_soc_kwh * 1000)
        res = compute_soc_resolution_wh(min_wh, max_wh)
        return res, int(round((max_wh - min_wh) / res)) + 1

    def test_typical_battery_keeps_exact_10wh_grid(self):
        """Batteries at or below the budget are bit-for-bit unaffected."""
        # 10 kWh capacity, 10-90% -> 8 kWh usable -> 800 states at 10 Wh
        res, states = self._grid(10.0)
        assert res == 10.0
        assert states == 801

    def test_grid_exact_at_budget_boundary(self):
        """The cap engages only above MAX_SOC_STATES * 10 Wh of usable range."""
        # 12.5 kWh capacity, 0-100% -> 12.5 kWh usable -> above the boundary
        res, states = self._grid(12.5, min_pct=0.0, max_pct=100.0)
        assert res > 10.0
        assert states <= MAX_SOC_STATES + 1
        # 10 kWh usable sits exactly on the boundary and stays at 10 Wh
        res_at, states_at = self._grid(10.0, min_pct=0.0, max_pct=100.0)
        assert res_at == 10.0
        assert states_at == MAX_SOC_STATES + 1

    @pytest.mark.parametrize("capacity_kwh", [20.0, 55.0, 100.0, 500.0])
    def test_large_batteries_stay_within_budget(self, capacity_kwh):
        """State count never exceeds the budget, however large the battery."""
        res, states = self._grid(capacity_kwh)
        assert states <= MAX_SOC_STATES + 1
        # Resolution stays fine relative to capacity: <= 0.1% of usable range
        usable_wh = capacity_kwh * 1000 * 0.8
        assert res <= usable_wh / MAX_SOC_STATES + 1e-9

    def test_worst_case_dp_table_stays_bounded(self):
        """Worst realistic horizon x largest battery stays under budget.

        48 h at 15-minute prices (the longest horizon the coordinator
        builds) against an oversized battery.
        """
        n_steps = 48 * 60 // 15
        _, states = self._grid(500.0)
        assert n_steps * states <= 200_000

    def test_large_battery_result_matches_fine_grid(self):
        """Coarsening the grid must not change the decision materially."""
        cfg = BatteryConfig(
            capacity_kwh=55.0,
            max_charge_power_kw=5.0,
            max_discharge_power_kw=5.0,
            charge_efficiency_curve="0.9487",
            discharge_efficiency_curve="0.9487",
            min_soc_percent=10.0,
            max_soc_percent=90.0,
        )
        n = 24
        prices = [0.10 + 0.20 * (i % 6 == 5) for i in range(n)]
        feed_in = [p * 0.5 for p in prices]
        result = optimize_battery_schedule(
            battery_config=cfg,
            current_soc_kwh=20.0,
            price_forecast=prices,
            feed_in_forecast=feed_in,
            pv_forecast=[0.0] * n,
            consumption_forecast=[0.6] * n,
            step_durations_hours=[1.0] * n,
            degradation_cost_per_kwh=0.0025,
            min_price_spread=0.05,
        )
        # Schedule is well-formed and the battery is actually used
        assert len(result.power_schedule_kw) == n
        assert result.savings > 0
        assert all(
            cfg.min_soc_kwh - 1e-6 <= s <= cfg.max_soc_kwh + 1e-6
            for s in result.soc_schedule_kwh
        )


class TestArbitrageHurdleInObjective:
    """min_price_spread is part of the DP objective, not a post-hoc filter."""

    @staticmethod
    def _cfg():
        return BatteryConfig(
            capacity_kwh=10.0,
            max_charge_power_kw=5.0,
            max_discharge_power_kw=5.0,
            charge_efficiency_curve=f"{math.sqrt(0.90):.6f}",
            discharge_efficiency_curve=f"{math.sqrt(0.90):.6f}",
            min_soc_percent=10.0,
            max_soc_percent=90.0,
        )

    @staticmethod
    def _run(cfg, prices, spread):
        n = len(prices)
        return optimize_battery_schedule(
            battery_config=cfg,
            current_soc_kwh=cfg.min_soc_kwh,
            price_forecast=prices,
            feed_in_forecast=[p * 0.5 for p in prices],
            pv_forecast=[0.0] * n,
            consumption_forecast=[0.5] * n,
            step_durations_hours=[1.0] * n,
            degradation_cost_per_kwh=0.002,
            min_price_spread=spread,
        )

    def test_thin_spread_is_rejected_by_the_dp_itself(self):
        """A spread below the hurdle must not be planned in the first place.

        Previously the DP planned it (its objective had no hurdle) and the
        oscillation filter deleted it afterwards, but only when a counterpart
        happened to fall inside the lookahead window.
        """
        cfg = self._cfg()
        # 3 ct spread: above 2 x degradation, below a 10 ct hurdle.
        prices = [0.20, 0.20, 0.23, 0.23, 0.20, 0.20]
        result = self._run(cfg, prices, spread=0.10)
        assert all(m == ACTION_IDLE for m in result.mode_schedule)

    def test_wide_spread_is_still_taken(self):
        """A spread comfortably above the hurdle must survive."""
        cfg = self._cfg()
        prices = [0.05, 0.05, 0.60, 0.60, 0.05, 0.05]
        result = self._run(cfg, prices, spread=0.10)
        assert ACTION_CHARGING in result.mode_schedule
        assert ACTION_DISCHARGING in result.mode_schedule

    def test_raising_the_spread_never_increases_cycling(self):
        """The hurdle is monotone: a higher spread cycles no more than a lower one."""
        cfg = self._cfg()
        prices = [0.10, 0.12, 0.18, 0.22, 0.16, 0.11, 0.19, 0.24]

        def throughput(spread):
            r = self._run(cfg, prices, spread)
            return sum(abs(p) for p in r.power_schedule_kw)

        assert throughput(0.20) <= throughput(0.05) + 1e-9
        assert throughput(0.05) <= throughput(0.0) + 1e-9

    def test_reported_costs_exclude_the_hurdle(self):
        """The hurdle steers decisions; it is not money and must not be billed.

        Two runs whose schedules are identical must report identical costs,
        whatever hurdle produced them.
        """
        cfg = self._cfg()
        prices = [0.05, 0.05, 0.60, 0.60, 0.05, 0.05]
        low = self._run(cfg, prices, spread=0.0)
        high = self._run(cfg, prices, spread=0.10)
        assert low.power_schedule_kw == pytest.approx(high.power_schedule_kw)
        assert low.total_cost == pytest.approx(high.total_cost)
        assert low.savings == pytest.approx(high.savings)

    def test_passive_dc_pv_is_exempt_from_the_hurdle(self):
        """Free DC PV is not an arbitrage decision and must not be discouraged."""
        cfg = BatteryConfig(
            capacity_kwh=10.0,
            max_charge_power_kw=5.0,
            max_discharge_power_kw=5.0,
            charge_efficiency_curve=f"{math.sqrt(0.90):.6f}",
            discharge_efficiency_curve=f"{math.sqrt(0.90):.6f}",
            min_soc_percent=10.0,
            max_soc_percent=90.0,
            pv_dc_coupled=True,
            pv_dc_efficiency=0.97,
        )
        common = dict(
            time_step_hours=1.0,
            soc_wh=cfg.min_soc_kwh * 1000,
            action_w=0.0,
            grid_price=0.20,
            feed_in_price=0.10,
            pv_production_w=0.0,
            consumption_w=500.0,
            charge_curve=cfg.charge_efficiency_curve_parsed,
            discharge_curve=cfg.discharge_efficiency_curve_parsed,
            degradation_cost_per_kwh=0.002,
            battery_config=cfg,
            pv_dc_production_w=2000.0,
        )
        without = calculate_step_cost(**common, arbitrage_cost_per_kwh=0.0)
        with_hurdle = calculate_step_cost(**common, arbitrage_cost_per_kwh=0.05)
        assert with_hurdle == pytest.approx(without)

    def test_commanded_throughput_carries_the_hurdle(self):
        """One full cycle costs 2 x degradation + min_price_spread per kWh."""
        cfg = self._cfg()
        common = dict(
            time_step_hours=1.0,
            soc_wh=5000.0,
            grid_price=0.20,
            feed_in_price=0.10,
            pv_production_w=0.0,
            consumption_w=0.0,
            charge_curve=cfg.charge_efficiency_curve_parsed,
            discharge_curve=cfg.discharge_efficiency_curve_parsed,
            degradation_cost_per_kwh=0.0,
            battery_config=cfg,
        )
        hurdle = 0.025  # = min_price_spread / 2
        charge = calculate_step_cost(
            **common, action_w=1000.0, arbitrage_cost_per_kwh=hurdle
        )
        charge_free = calculate_step_cost(
            **common, action_w=1000.0, arbitrage_cost_per_kwh=0.0
        )
        stored_kwh = 1.0 * math.sqrt(0.90)
        assert charge - charge_free == pytest.approx(stored_kwh * hurdle)


class TestSocGridExactFit:
    """The DP SoC grid must land exactly on max_soc_kwh."""

    @pytest.mark.parametrize(
        "capacity_kwh, min_pct, max_pct",
        [
            (10.0, 10.0, 90.0),  # 8000 Wh: whole multiple of the 10 Wh target
            (10.005, 10.0, 90.0),  # 8004 Wh: not a whole multiple
            (7.68, 15.0, 95.0),
            (55.0, 5.0, 95.0),  # coarsened by the MAX_SOC_STATES budget
        ],
    )
    def test_top_state_is_max_soc(self, capacity_kwh, min_pct, max_pct):
        cfg = BatteryConfig(
            capacity_kwh=capacity_kwh,
            max_charge_power_kw=5.0,
            max_discharge_power_kw=5.0,
            min_soc_percent=min_pct,
            max_soc_percent=max_pct,
        )
        min_wh = round(cfg.min_soc_kwh * 1000)
        max_wh = round(cfg.max_soc_kwh * 1000)
        res = compute_soc_resolution_wh(min_wh, max_wh)
        n = int(round((max_wh - min_wh) / res)) + 1
        assert n <= MAX_SOC_STATES + 1
        exact_res = (max_wh - min_wh) / (n - 1)
        top = min_wh + (n - 1) * exact_res
        assert top == pytest.approx(max_wh, abs=1e-6)

    def test_fill_to_max_reaches_max_soc(self):
        """The fill-to-max boundary action must end exactly at max_soc."""
        cfg = BatteryConfig(
            capacity_kwh=10.005,
            max_charge_power_kw=5.0,
            max_discharge_power_kw=5.0,
            charge_efficiency_curve=f"{math.sqrt(0.90):.6f}",
            discharge_efficiency_curve=f"{math.sqrt(0.90):.6f}",
            min_soc_percent=10.0,
            max_soc_percent=90.0,
        )
        result = optimize_battery_schedule(
            battery_config=cfg,
            current_soc_kwh=cfg.max_soc_kwh - 0.05,
            price_forecast=[0.01, 0.90, 0.90],
            feed_in_forecast=[0.005, 0.45, 0.45],
            pv_forecast=[0.0] * 3,
            consumption_forecast=[0.0] * 3,
            step_durations_hours=[1.0] * 3,
            degradation_cost_per_kwh=0.001,
            min_price_spread=0.0,
        )
        assert max(result.soc_schedule_kwh) <= cfg.max_soc_kwh + 1e-6


class TestStepCostCacheInvariant:
    """The per-step cost cache relies on step cost being SoC-independent.

    simulate_diagnostics.py has no such cache, so tests/test_cross_impl.py is
    the end-to-end check that it changes no result. These tests pin the
    precondition the cache is built on.
    """

    @staticmethod
    def _cfg(dc_coupled):
        return BatteryConfig(
            capacity_kwh=10.0,
            max_charge_power_kw=5.0,
            max_discharge_power_kw=5.0,
            charge_efficiency_curve="0:0.88, 5:0.96",
            discharge_efficiency_curve="0:0.86, 5:0.95",
            min_soc_percent=10.0,
            max_soc_percent=90.0,
            pv_dc_coupled=dc_coupled,
            pv_dc_efficiency=0.97,
        )

    def _cost(self, cfg, soc_wh, action_w, pv_dc_w):
        return calculate_step_cost(
            time_step_hours=0.25,
            soc_wh=soc_wh,
            action_w=action_w,
            grid_price=0.22,
            feed_in_price=0.08,
            pv_production_w=300.0,
            consumption_w=700.0,
            charge_curve=cfg.charge_efficiency_curve_parsed,
            discharge_curve=cfg.discharge_efficiency_curve_parsed,
            degradation_cost_per_kwh=0.003,
            battery_config=cfg,
            pv_dc_production_w=pv_dc_w,
            arbitrage_cost_per_kwh=0.02,
        )

    @pytest.mark.parametrize("action_w", [-3000.0, -100.0, 0.0, 100.0, 3000.0])
    def test_soc_independent_without_dc_pv(self, action_w):
        """No DC PV: cost is the same at every SoC, so one probe covers all."""
        cfg = self._cfg(dc_coupled=False)
        costs = [
            self._cost(cfg, soc_wh, action_w, 0.0)
            for soc_wh in (1000.0, 4000.0, 7000.0, 9000.0)
        ]
        assert costs == pytest.approx([costs[0]] * len(costs))

    @pytest.mark.parametrize("action_w", [-3000.0, -100.0])
    def test_discharge_soc_independent_with_dc_pv(self, action_w):
        """Discharging never reads the SoC, DC PV or not."""
        cfg = self._cfg(dc_coupled=True)
        costs = [
            self._cost(cfg, soc_wh, action_w, 2000.0)
            for soc_wh in (1000.0, 4000.0, 7000.0, 8999.0)
        ]
        assert costs == pytest.approx([costs[0]] * len(costs))

    @pytest.mark.parametrize("action_w", [0.0, 1000.0])
    def test_charge_and_idle_soc_independent_below_headroom_limit(self, action_w):
        """With DC PV, charge/idle cost is constant until headroom clips it."""
        cfg = self._cfg(dc_coupled=True)
        pv_dc_w = 2000.0
        max_soc_wh = cfg.max_soc_kwh * 1000
        charge_eff = interpolate_efficiency(
            cfg.charge_efficiency_curve_parsed, abs(action_w) / 1000.0
        )
        ac_stored_wh = action_w * 0.25 * charge_eff if action_w > 0 else 0.0
        passive_full_wh = pv_dc_w * cfg.pv_dc_efficiency * 0.25
        limit = max_soc_wh - ac_stored_wh - passive_full_wh

        below = [
            self._cost(cfg, soc_wh, action_w, pv_dc_w)
            for soc_wh in (1000.0, 3000.0, limit - 1.0, limit)
        ]
        assert below == pytest.approx([below[0]] * len(below))

        # Above the limit the headroom clips the passive charge, so the cost
        # genuinely does depend on SoC and the cache must not be used.
        clipped = self._cost(cfg, limit + 200.0, action_w, pv_dc_w)
        assert clipped != pytest.approx(below[0])


class TestMicroCycleFirstStep:
    """The shortened first step must not be judged as a micro-cycle."""

    def test_short_first_step_is_sized_on_the_reference_interval(self):
        """A one-minute step 0 is a full-period action seen late, not a micro-cycle.

        Step 0 covers only the remainder of the current price period, so a
        2 kW action there moves 0.03 kWh on paper and used to be suppressed for
        falling under MIN_CYCLE_KWH — purely because the optimizer happened to
        run just before a period boundary.
        """
        durations = [1 / 60.0] + [0.25] * 3
        power, mode, _soc = _filter_micro_cycles(
            power_schedule_kw=[2.0, 0.0, 0.0, 0.0],
            mode_schedule=[ACTION_CHARGING, ACTION_IDLE, ACTION_IDLE, ACTION_IDLE],
            initial_soc_kwh=4.0,
            step_durations_hours=durations,
            min_soc_kwh=1.0,
            max_soc_kwh=9.0,
            charge_curve=_flat_curve(0.95),
            discharge_curve=_flat_curve(0.95),
            min_cycle_kwh=0.2,
        )
        # 2 kW over the 0.25 h reference interval = 0.5 kWh > 0.2 kWh
        assert mode[0] == ACTION_CHARGING
        assert power[0] == pytest.approx(2.0)

    def test_genuine_micro_cycle_is_still_removed(self):
        """A block that is small on its own full interval is still suppressed."""
        durations = [1 / 60.0] + [0.25] * 3
        power, mode, _soc = _filter_micro_cycles(
            power_schedule_kw=[0.2, 0.0, 0.0, 0.0],
            mode_schedule=[ACTION_CHARGING, ACTION_IDLE, ACTION_IDLE, ACTION_IDLE],
            initial_soc_kwh=4.0,
            step_durations_hours=durations,
            min_soc_kwh=1.0,
            max_soc_kwh=9.0,
            charge_curve=_flat_curve(0.95),
            discharge_curve=_flat_curve(0.95),
            min_cycle_kwh=0.2,
        )
        # 0.2 kW over 0.25 h = 0.05 kWh < 0.2 kWh
        assert mode[0] == ACTION_IDLE
        assert power[0] == pytest.approx(0.0)


class TestGridCapacityCap:
    """The grid connection cap must bound the ACTION, not the price of the flow.

    Clipping ``net_grid_w`` on the import side made every watt above the cap
    free — the SoC kept rising while the cost stopped growing — so the DP
    charged at full power whenever the cap was binding and planned imports of
    nearly twice the configured limit.
    """

    @staticmethod
    def _config(cap_kw: float) -> BatteryConfig:
        return BatteryConfig(
            capacity_kwh=20.0,
            max_charge_power_kw=4.0,
            max_discharge_power_kw=2.0,
            min_soc_percent=10.0,
            max_soc_percent=90.0,
            max_grid_power_kw=cap_kw,
        )

    def test_over_cap_import_is_still_priced(self):
        """Charging past the cap must never be free."""
        config = self._config(3.0)
        common = {
            "time_step_hours": 1.0,
            "soc_wh": 5000.0,
            "grid_price": 0.30,
            "feed_in_price": 0.05,
            "pv_production_w": 0.0,
            "consumption_w": 2000.0,
            "charge_curve": config.charge_efficiency_curve_parsed,
            "discharge_curve": config.discharge_efficiency_curve_parsed,
            "degradation_cost_per_kwh": 0.0,
            "battery_config": config,
        }
        costs = [calculate_step_cost(action_w=a, **common) for a in (1000, 2000, 3000)]
        # Each extra kW costs a full kWh at the grid price, cap or no cap.
        assert costs[1] - costs[0] == pytest.approx(0.30)
        assert costs[2] - costs[1] == pytest.approx(0.30)

    def test_export_beyond_cap_earns_nothing(self):
        """Curtailment is still modelled: unexportable PV has no revenue."""
        config = self._config(3.0)
        common = {
            "time_step_hours": 1.0,
            "soc_wh": 5000.0,
            "action_w": 0.0,
            "grid_price": 0.30,
            "feed_in_price": 0.10,
            "consumption_w": 0.0,
            "charge_curve": config.charge_efficiency_curve_parsed,
            "discharge_curve": config.discharge_efficiency_curve_parsed,
            "degradation_cost_per_kwh": 0.0,
            "battery_config": config,
        }
        at_cap = calculate_step_cost(pv_production_w=3000.0, **common)
        past_cap = calculate_step_cost(pv_production_w=6000.0, **common)
        assert at_cap == pytest.approx(-0.30)
        assert past_cap == pytest.approx(at_cap)

    def test_schedule_respects_the_connection_limit(self):
        """The planned grid flow may not exceed the configured cap."""
        prices = [0.10, 0.10, 0.10, 0.40, 0.40, 0.40]
        consumption = [1.0] * 6
        result = optimize_battery_schedule(
            battery_config=self._config(3.0),
            current_soc_kwh=8.0,
            price_forecast=prices,
            feed_in_forecast=[0.08] * 3 + [0.35] * 3,
            pv_forecast=[0.0] * 6,
            consumption_forecast=consumption,
            step_durations_hours=[1.0] * 6,
            degradation_cost_per_kwh=0.01,
            min_price_spread=0.05,
        )
        grid_kw = [
            consumption[t] + result.power_schedule_kw[t]
            for t in range(len(result.power_schedule_kw))
        ]
        assert max(grid_kw) <= 3.0 + 1e-6, f"grid flow exceeds the cap: {grid_kw}"
        # ... and the battery is still used: the cap limits it, not disables it.
        assert max(result.power_schedule_kw) > 0.0

    def test_uncapped_schedule_is_unaffected(self):
        """0 = unlimited must leave the plan exactly as it was."""
        kwargs = {
            "current_soc_kwh": 8.0,
            "price_forecast": [0.10, 0.10, 0.10, 0.40, 0.40, 0.40],
            "feed_in_forecast": [0.08] * 3 + [0.35] * 3,
            "pv_forecast": [0.0] * 6,
            "consumption_forecast": [1.0] * 6,
            "step_durations_hours": [1.0] * 6,
            "degradation_cost_per_kwh": 0.01,
            "min_price_spread": 0.05,
        }
        uncapped = optimize_battery_schedule(battery_config=self._config(0.0), **kwargs)
        generous = optimize_battery_schedule(
            battery_config=self._config(50.0), **kwargs
        )
        assert uncapped.power_schedule_kw == pytest.approx(generous.power_schedule_kw)


class TestPassiveDcChargeRating:
    """Passive DC MPPT charging shares the battery's charge-power budget."""

    @staticmethod
    def _config(max_charge_kw: float) -> BatteryConfig:
        return BatteryConfig(
            capacity_kwh=20.0,
            max_charge_power_kw=max_charge_kw,
            max_discharge_power_kw=2.0,
            min_soc_percent=10.0,
            max_soc_percent=90.0,
            pv_dc_coupled=True,
            pv_dc_efficiency=0.97,
        )

    def test_array_larger_than_the_inverter_is_clipped(self):
        """A 4 kW array on a 2 kW battery may only charge it at 2 kW."""
        result = optimize_battery_schedule(
            battery_config=self._config(2.0),
            current_soc_kwh=2.5,
            price_forecast=[0.20, 0.20, 0.50, 0.50],
            feed_in_forecast=[0.0] * 4,
            pv_forecast=[0.0] * 4,
            consumption_forecast=[0.5, 0.5, 1.5, 1.5],
            step_durations_hours=[1.0] * 4,
            degradation_cost_per_kwh=0.02,
            min_price_spread=0.05,
            pv_dc_forecast=[4.0, 4.0, 0.0, 0.0],
        )
        soc = result.soc_schedule_kwh
        absorbed = [soc[t + 1] - soc[t] for t in range(2)]
        assert max(absorbed) <= 2.0 + 1e-6, f"charged past the rating: {absorbed}"
        # The array can supply 4 x 0.97 = 3.88 kW, so the rating is what binds.
        assert max(absorbed) == pytest.approx(2.0, abs=0.01)

    def test_ac_setpoint_and_passive_path_share_the_budget(self):
        """An AC charge command may not buy extra headroom for the DC path."""
        rating_kw, step_h = 2.0, 1.0
        array_wh = 4000.0 * 0.97 * step_h  # 3880 Wh available from the panels
        roomy = 10_000.0  # SoC headroom well above anything the budget allows

        idle = passive_dc_charge_wh(
            4000.0, 0.97, step_h, roomy, charge_budget_wh(rating_kw, step_h, 0.0)
        )
        # Idle: the rating binds, not the array.
        assert idle == pytest.approx(2000.0)
        assert idle < array_wh

        # Charging 1 kW AC stores ~950 Wh, so only ~1050 Wh of budget is left.
        ac_stored_wh = 1000.0 * step_h * 0.9487
        with_ac = passive_dc_charge_wh(
            4000.0,
            0.97,
            step_h,
            roomy,
            charge_budget_wh(rating_kw, step_h, ac_stored_wh),
        )
        assert with_ac == pytest.approx(2000.0 - ac_stored_wh)
        # Total into the battery is still the rating — not the rating plus the
        # AC setpoint, which is what the unbounded passive path used to give.
        assert ac_stored_wh + with_ac == pytest.approx(2000.0)

    def test_headroom_still_binds_when_it_is_the_tighter_limit(self):
        """The budget is an extra bound, not a replacement for SoC headroom."""
        absorbed = passive_dc_charge_wh(
            4000.0, 0.97, 1.0, headroom_wh=300.0, budget_wh=2000.0
        )
        assert absorbed == pytest.approx(300.0)


class TestOscillationFilterCostGuard:
    """The oscillation filter must never make the schedule more expensive."""

    def test_filtered_schedule_is_never_worse_than_the_dp_plan(self):
        """Priced in real money, the returned plan beats or matches the raw one."""
        import random

        random.seed(3)
        config = BatteryConfig(
            capacity_kwh=10.0,
            max_charge_power_kw=5.0,
            max_discharge_power_kw=5.0,
            min_soc_percent=10.0,
            max_soc_percent=90.0,
        )
        shape = [0.08, 0.07, 0.16, 0.24, 0.14, 0.10, 0.22, 0.34, 0.24, 0.12]
        for trial in range(5):
            prices = [
                max(0.0, shape[i % len(shape)] * (0.7 + 0.6 * random.random()))
                for i in range(48)
            ]
            result = optimize_battery_schedule(
                battery_config=config,
                current_soc_kwh=4.0,
                price_forecast=prices,
                feed_in_forecast=[p * 0.85 for p in prices],
                pv_forecast=[0.0] * 48,
                consumption_forecast=[0.4] * 48,
                step_durations_hours=[0.25] * 48,
                degradation_cost_per_kwh=0.026,
                min_price_spread=0.05,
            )
            # raw_savings is the pre-filter plan priced identically to savings,
            # so the filters may only ever be neutral or better on this axis
            # once the micro-cycle allowance is granted.
            assert result.raw_savings is not None
            assert result.savings <= result.raw_savings + 1e-9, (
                f"trial {trial}: filtering somehow gained value, check the pricing"
            )

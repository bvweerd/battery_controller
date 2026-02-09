"""Tests for battery_model.py."""

import math
import pytest

from custom_components.battery_controller.battery_model import (
    BatteryConfig,
    BatteryState,
    calculate_efficiency,
    calculate_new_soc,
    calculate_max_charge_power,
    calculate_max_discharge_power,
    should_cycle,
    calculate_degradation_cost_per_kwh,
)


class TestBatteryConfig:
    """Tests for BatteryConfig dataclass."""

    def test_default_config(self):
        config = BatteryConfig()
        assert config.capacity_kwh == 10.0
        assert config.max_charge_power_kw == 5.0
        assert config.round_trip_efficiency == 0.90

    def test_derived_values(self):
        config = BatteryConfig(
            capacity_kwh=10.0, min_soc_percent=10.0, max_soc_percent=90.0
        )
        assert config.min_soc_kwh == pytest.approx(1.0)
        assert config.max_soc_kwh == pytest.approx(9.0)
        assert config.charge_efficiency == pytest.approx(math.sqrt(0.90))
        assert config.discharge_efficiency == pytest.approx(math.sqrt(0.90))

    def test_usable_capacity_auto(self):
        config = BatteryConfig(
            capacity_kwh=10.0, min_soc_percent=10.0, max_soc_percent=90.0
        )
        assert config.usable_capacity_kwh == pytest.approx(8.0)

    def test_usable_capacity_override(self):
        config = BatteryConfig(capacity_kwh=10.0, usable_capacity_kwh=7.5)
        assert config.usable_capacity_kwh == pytest.approx(7.5)

    def test_dc_coupled_defaults(self):
        config = BatteryConfig()
        assert config.pv_dc_coupled is False
        assert config.pv_dc_peak_power_kwp == 0.0
        assert config.pv_dc_efficiency == 0.97

    def test_dc_coupled_config(self):
        config = BatteryConfig(
            pv_dc_coupled=True,
            pv_dc_peak_power_kwp=3.0,
            pv_dc_efficiency=0.96,
        )
        assert config.pv_dc_coupled is True
        assert config.pv_dc_peak_power_kwp == 3.0
        assert config.pv_dc_efficiency == 0.96

    def test_from_config(self):
        ha_config = {
            "capacity_kwh": 15.0,
            "max_charge_power_kw": 7.5,
            "max_discharge_power_kw": 7.5,
            "round_trip_efficiency": 0.92,
            "min_soc_percent": 5.0,
            "max_soc_percent": 95.0,
            "pv_dc_coupled": True,
            "pv_dc_peak_power_kwp": 4.0,
            "pv_dc_efficiency": 0.97,
        }
        config = BatteryConfig.from_config(ha_config)
        assert config.capacity_kwh == 15.0
        assert config.max_charge_power_kw == 7.5
        assert config.round_trip_efficiency == 0.92
        assert config.min_soc_kwh == pytest.approx(0.75)
        assert config.max_soc_kwh == pytest.approx(14.25)
        assert config.pv_dc_coupled is True
        assert config.pv_dc_peak_power_kwp == 4.0

    def test_from_config_defaults(self):
        config = BatteryConfig.from_config({})
        assert config.capacity_kwh == 10.0
        assert config.round_trip_efficiency == 0.90


class TestBatteryState:
    """Tests for BatteryState dataclass."""

    def test_from_soc_kwh(self):
        state = BatteryState.from_soc_kwh(5.0, 10.0)
        assert state.soc_kwh == 5.0
        assert state.soc_percent == 50.0

    def test_from_soc_percent(self):
        state = BatteryState.from_soc_percent(75.0, 10.0)
        assert state.soc_kwh == 7.5
        assert state.soc_percent == 75.0

    def test_from_soc_kwh_zero_capacity(self):
        state = BatteryState.from_soc_kwh(0.0, 0.0)
        assert state.soc_percent == 0.0


class TestCalculateEfficiency:
    """Tests for calculate_efficiency function."""

    def test_base_efficiency_charging(self):
        config = BatteryConfig(round_trip_efficiency=0.90)
        eff = calculate_efficiency(2.0, 50.0, config)
        # At 50% SoC, 0.4C rate -> no penalties
        assert eff == pytest.approx(math.sqrt(0.90), abs=0.01)

    def test_base_efficiency_discharging(self):
        config = BatteryConfig(round_trip_efficiency=0.90)
        eff = calculate_efficiency(-2.0, 50.0, config)
        assert eff == pytest.approx(math.sqrt(0.90), abs=0.01)

    def test_high_c_rate_penalty(self):
        config = BatteryConfig(capacity_kwh=10.0, round_trip_efficiency=0.90)
        eff_low = calculate_efficiency(2.0, 50.0, config)  # 0.2C
        eff_high = calculate_efficiency(8.0, 50.0, config)  # 0.8C
        assert eff_high < eff_low

    def test_extreme_soc_penalty(self):
        config = BatteryConfig(round_trip_efficiency=0.90)
        eff_mid = calculate_efficiency(2.0, 50.0, config)
        eff_low = calculate_efficiency(2.0, 5.0, config)
        eff_high = calculate_efficiency(2.0, 95.0, config)
        assert eff_low < eff_mid
        assert eff_high < eff_mid


class TestCalculateNewSoc:
    """Tests for calculate_new_soc function."""

    def test_charging(self):
        config = BatteryConfig(capacity_kwh=10.0, max_soc_percent=90.0)
        new_soc, energy = calculate_new_soc(5.0, 2.0, 1.0, config)
        # 2 kW * 1h * efficiency = ~1.9 kWh stored
        assert new_soc > 5.0
        assert energy > 0

    def test_discharging(self):
        config = BatteryConfig(capacity_kwh=10.0, min_soc_percent=10.0)
        new_soc, energy = calculate_new_soc(5.0, -2.0, 1.0, config)
        assert new_soc < 5.0
        assert energy > 0

    def test_idle(self):
        config = BatteryConfig()
        new_soc, energy = calculate_new_soc(5.0, 0.0, 1.0, config)
        assert new_soc == 5.0
        assert energy == 0.0

    def test_charge_clamp_max(self):
        config = BatteryConfig(capacity_kwh=10.0, max_soc_percent=90.0)
        # Try to charge 8.5 kWh battery to above max (9.0 kWh)
        new_soc, _ = calculate_new_soc(8.5, 5.0, 1.0, config)
        assert new_soc <= 9.0

    def test_discharge_clamp_min(self):
        config = BatteryConfig(capacity_kwh=10.0, min_soc_percent=10.0)
        # Try to discharge 1.5 kWh battery below min (1.0 kWh)
        new_soc, _ = calculate_new_soc(1.5, -5.0, 1.0, config)
        assert new_soc >= 1.0


class TestMaxPower:
    """Tests for max charge/discharge power functions."""

    def test_max_charge_at_low_soc(self):
        config = BatteryConfig(
            capacity_kwh=10.0, max_charge_power_kw=5.0, max_soc_percent=90.0
        )
        power = calculate_max_charge_power(2.0, 1.0, config)
        # Lots of headroom -> should be limited by inverter
        assert power == pytest.approx(5.0, abs=0.5)

    def test_max_charge_near_full(self):
        config = BatteryConfig(
            capacity_kwh=10.0, max_charge_power_kw=5.0, max_soc_percent=90.0
        )
        power = calculate_max_charge_power(8.8, 1.0, config)
        # Only 0.2 kWh headroom -> low power
        assert power < 1.0

    def test_max_charge_at_full(self):
        config = BatteryConfig(
            capacity_kwh=10.0, max_charge_power_kw=5.0, max_soc_percent=90.0
        )
        power = calculate_max_charge_power(9.0, 1.0, config)
        assert power == 0.0

    def test_max_discharge_at_high_soc(self):
        config = BatteryConfig(
            capacity_kwh=10.0, max_discharge_power_kw=5.0, min_soc_percent=10.0
        )
        power = calculate_max_discharge_power(8.0, 1.0, config)
        assert power == pytest.approx(5.0, abs=0.5)

    def test_max_discharge_at_min(self):
        config = BatteryConfig(
            capacity_kwh=10.0, max_discharge_power_kw=5.0, min_soc_percent=10.0
        )
        power = calculate_max_discharge_power(1.0, 1.0, config)
        assert power == 0.0


class TestShouldCycle:
    """Tests for should_cycle function."""

    def test_profitable_cycle(self):
        # Buy at 0.05, sell at 0.20, RTE 0.90 -> profitable
        assert should_cycle(0.05, 0.20, 0.90, 0.03) is True

    def test_unprofitable_cycle(self):
        # Buy at 0.10, sell at 0.12, RTE 0.90 -> not profitable
        assert should_cycle(0.10, 0.12, 0.90, 0.03) is False

    def test_marginal_cycle(self):
        # At RTE=0.90, degradation=0.03:
        # min_sell = 0.10 / 0.90 + 0.06 = 0.171
        assert should_cycle(0.10, 0.17, 0.90, 0.03) is False
        assert should_cycle(0.10, 0.18, 0.90, 0.03) is True


class TestDegradationCost:
    """Tests for calculate_degradation_cost_per_kwh function."""

    def test_default_values(self):
        cost = calculate_degradation_cost_per_kwh()
        # 500 / 6000 / (2 * 0.8) = 0.052
        assert cost == pytest.approx(0.052, abs=0.001)

    def test_cheap_battery(self):
        cost = calculate_degradation_cost_per_kwh(
            replacement_cost_per_kwh=200.0,
            lifecycle_cycles=10000,
        )
        assert cost < 0.02

    def test_expensive_battery(self):
        cost = calculate_degradation_cost_per_kwh(
            replacement_cost_per_kwh=800.0,
            lifecycle_cycles=3000,
        )
        assert cost > 0.10

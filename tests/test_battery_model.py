"""Tests for battery_model.py."""

import math
import pytest

from custom_components.battery_controller.battery_model import (
    BatteryConfig,
    BatteryState,
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

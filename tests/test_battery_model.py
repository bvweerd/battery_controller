"""Tests for battery_model.py."""

import pytest

from custom_components.battery_controller.battery_model import (
    BatteryConfig,
    BatteryState,
    aggregate_battery_configs,
)


class TestBatteryConfig:
    """Tests for BatteryConfig dataclass."""

    def test_default_config(self):
        config = BatteryConfig()
        assert config.capacity_kwh == 10.0
        assert config.max_charge_power_kw == 5.0
        # Default curve is "0.9487" (= sqrt 0.90) per direction → RTE ≈ 0.90,
        # matching the pre-curve scalar default.
        assert config.charge_efficiency == pytest.approx(0.9487)
        assert config.discharge_efficiency == pytest.approx(0.9487)
        assert config.round_trip_efficiency == pytest.approx(0.90, abs=1e-3)

    def test_derived_values(self):
        config = BatteryConfig(
            capacity_kwh=10.0, min_soc_percent=10.0, max_soc_percent=90.0
        )
        assert config.min_soc_kwh == pytest.approx(1.0)
        assert config.max_soc_kwh == pytest.approx(9.0)
        # Default curve "0.9487" → scalar at zero power = 0.9487
        assert config.charge_efficiency == pytest.approx(0.9487)
        assert config.discharge_efficiency == pytest.approx(0.9487)

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
        assert config.round_trip_efficiency == pytest.approx(0.92, abs=1e-4)
        assert config.min_soc_kwh == pytest.approx(0.75)
        assert config.max_soc_kwh == pytest.approx(14.25)
        assert config.pv_dc_coupled is True
        assert config.pv_dc_peak_power_kwp == 4.0

    def test_from_config_defaults(self):
        config = BatteryConfig.from_config({})
        assert config.capacity_kwh == 10.0
        assert config.round_trip_efficiency == pytest.approx(0.90, abs=1e-4)

    def test_derating_defaults_disabled(self):
        """Default config has no derating (thresholds at 100% / 0%)."""
        config = BatteryConfig(capacity_kwh=10.0)
        assert config.high_soc_charge_threshold_pct == 100.0
        assert config.high_soc_max_charge_kw == 0.0
        assert config.low_soc_discharge_threshold_pct == 0.0
        assert config.low_soc_max_discharge_kw == 0.0

    def test_max_charge_at_soc_no_derating(self):
        """Without derating configured, max_charge_at_soc returns nominal max."""
        config = BatteryConfig(capacity_kwh=10.0, max_charge_power_kw=1.2)
        assert config.max_charge_at_soc(9.5) == pytest.approx(1.2)
        assert config.max_charge_at_soc(5.0) == pytest.approx(1.2)
        assert config.max_charge_at_soc(0.5) == pytest.approx(1.2)

    def test_max_charge_at_soc_derated_above_threshold(self):
        """At or above threshold SoC, derated limit is returned."""
        config = BatteryConfig(
            capacity_kwh=10.0,
            max_charge_power_kw=1.2,
            high_soc_charge_threshold_pct=95.0,
            high_soc_max_charge_kw=0.45,
        )
        # Below threshold → nominal
        assert config.max_charge_at_soc(9.0) == pytest.approx(1.2)  # 90%
        assert config.max_charge_at_soc(9.49) == pytest.approx(1.2)  # 94.9%
        # At threshold → derated
        assert config.max_charge_at_soc(9.5) == pytest.approx(0.45)  # exactly 95%
        # Above threshold → derated
        assert config.max_charge_at_soc(9.8) == pytest.approx(0.45)  # 98%

    def test_max_discharge_at_soc_no_derating(self):
        """Without derating configured, max_discharge_at_soc returns nominal max."""
        config = BatteryConfig(capacity_kwh=10.0, max_discharge_power_kw=1.2)
        assert config.max_discharge_at_soc(1.5) == pytest.approx(1.2)
        assert config.max_discharge_at_soc(5.0) == pytest.approx(1.2)
        assert config.max_discharge_at_soc(9.0) == pytest.approx(1.2)

    def test_max_discharge_at_soc_derated_below_threshold(self):
        """At or below threshold SoC, derated limit is returned."""
        config = BatteryConfig(
            capacity_kwh=10.0,
            max_discharge_power_kw=1.2,
            low_soc_discharge_threshold_pct=15.0,
            low_soc_max_discharge_kw=0.38,
        )
        # Above threshold → nominal
        assert config.max_discharge_at_soc(2.0) == pytest.approx(1.2)  # 20%
        assert config.max_discharge_at_soc(1.51) == pytest.approx(1.2)  # 15.1%
        # At threshold → derated
        assert config.max_discharge_at_soc(1.5) == pytest.approx(0.38)  # exactly 15%
        # Below threshold → derated
        assert config.max_discharge_at_soc(1.0) == pytest.approx(0.38)  # 10%

    def test_from_subentry_with_derating(self):
        """from_subentry reads SoC-dependent derating fields."""
        data = {
            "capacity_kwh": 5.2,
            "max_charge_power_kw": 1.2,
            "max_discharge_power_kw": 1.2,
            "round_trip_efficiency": 0.92,
            "min_soc_percent": 10.0,
            "max_soc_percent": 100.0,
            "pv_dc_efficiency": 0.97,
            "battery_soc_sensor": "sensor.batt_soc",
            "high_soc_charge_threshold_pct": 95.0,
            "high_soc_max_charge_kw": 0.45,
            "low_soc_discharge_threshold_pct": 15.0,
            "low_soc_max_discharge_kw": 0.38,
        }
        config = BatteryConfig.from_subentry(data)
        assert config.high_soc_charge_threshold_pct == 95.0
        assert config.high_soc_max_charge_kw == pytest.approx(0.45)
        assert config.low_soc_discharge_threshold_pct == 15.0
        assert config.low_soc_max_discharge_kw == pytest.approx(0.38)

    def test_from_subentry_derating_defaults(self):
        """from_subentry without derating keys defaults to disabled."""
        data = {
            "capacity_kwh": 5.2,
            "max_charge_power_kw": 1.2,
            "max_discharge_power_kw": 1.2,
            "round_trip_efficiency": 0.92,
            "min_soc_percent": 10.0,
            "max_soc_percent": 100.0,
            "pv_dc_efficiency": 0.97,
            "battery_soc_sensor": "sensor.batt_soc",
        }
        config = BatteryConfig.from_subentry(data)
        assert config.high_soc_max_charge_kw == 0.0
        assert config.low_soc_max_discharge_kw == 0.0


class TestAggregateBatteryConfigs:
    """Tests for aggregate_battery_configs."""

    def test_single_config_passthrough(self):
        config = BatteryConfig(capacity_kwh=5.0, max_charge_power_kw=1.2)
        result = aggregate_battery_configs([config])
        assert result == config

    def test_single_config_is_a_copy(self):
        """The aggregate must not alias the single input config.

        The coordinator overlays entry-level settings (DC coupling, grid cap)
        onto the aggregate; sharing the object would write them straight into
        the individual battery's own config.
        """
        config = BatteryConfig(capacity_kwh=5.0, max_charge_power_kw=1.2)
        result = aggregate_battery_configs([config])
        assert result is not config
        result.pv_dc_coupled = True
        result.max_grid_power_kw = 17.0
        assert config.pv_dc_coupled is False
        assert config.max_grid_power_kw == 0.0

    def test_empty_returns_default(self):
        result = aggregate_battery_configs([])
        assert result.capacity_kwh == 10.0  # BatteryConfig default

    def test_power_limits_summed(self):
        a = BatteryConfig(
            capacity_kwh=5.0, max_charge_power_kw=1.2, max_discharge_power_kw=1.2
        )
        b = BatteryConfig(
            capacity_kwh=5.0, max_charge_power_kw=1.0, max_discharge_power_kw=0.8
        )
        result = aggregate_battery_configs([a, b])
        assert result.max_charge_power_kw == pytest.approx(2.2)
        assert result.max_discharge_power_kw == pytest.approx(2.0)

    def test_derating_aggregated(self):
        """Derated powers are summed; thresholds are capacity-weighted averages."""
        a = BatteryConfig(
            capacity_kwh=5.0,
            max_charge_power_kw=1.2,
            max_discharge_power_kw=1.2,
            high_soc_charge_threshold_pct=95.0,
            high_soc_max_charge_kw=0.45,
            low_soc_discharge_threshold_pct=15.0,
            low_soc_max_discharge_kw=0.38,
        )
        b = BatteryConfig(
            capacity_kwh=5.0,
            max_charge_power_kw=1.2,
            max_discharge_power_kw=1.2,
            high_soc_charge_threshold_pct=95.0,
            high_soc_max_charge_kw=0.45,
            low_soc_discharge_threshold_pct=15.0,
            low_soc_max_discharge_kw=0.38,
        )
        result = aggregate_battery_configs([a, b])
        assert result.high_soc_charge_threshold_pct == pytest.approx(95.0)
        assert result.high_soc_max_charge_kw == pytest.approx(0.90)
        assert result.low_soc_discharge_threshold_pct == pytest.approx(15.0)
        assert result.low_soc_max_discharge_kw == pytest.approx(0.76)

    def test_no_derating_preserved_on_aggregate(self):
        """Two configs without derating stay without derating after aggregation."""
        a = BatteryConfig(
            capacity_kwh=5.0, max_charge_power_kw=1.2, max_discharge_power_kw=1.2
        )
        b = BatteryConfig(
            capacity_kwh=5.0, max_charge_power_kw=1.0, max_discharge_power_kw=1.0
        )
        result = aggregate_battery_configs([a, b])
        assert result.high_soc_max_charge_kw == 0.0
        assert result.low_soc_max_discharge_kw == 0.0


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

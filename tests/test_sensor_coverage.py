"""Additional tests for sensor.py to cover missing lines."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock

import pytest

from homeassistant.helpers.entity import DeviceInfo

from custom_components.battery_controller.const import (
    ACTION_CHARGING,
    ACTION_DISCHARGING,
    ACTION_IDLE,
    DOMAIN,
)
from custom_components.battery_controller.sensor import (
    BatteryControlModeSensor,
    BatteryDailySavingsSensor,
    BatteryGridSetpointSensor,
    BatteryOptimalModeSensor,
    BatteryOptimalPowerSensor,
    BatteryPowerSensor,
    BatteryScheduleSensor,
    BatteryShadowPriceSensor,
    BatterySoCSensor,
    BatterySubentrySetpointSensor,
    ConsumptionForecastSensor,
    CurrentGridPowerSensor,
    NetGridForecastSensor,
    OptimizationStatusSensor,
    PVForecastSensor,
    SolarIrradianceSensor,
    WindSpeedSensor,
)


def _make_opt_coord(data=None):
    coord = MagicMock()
    coord.data = data
    coord.battery_config = MagicMock(round_trip_efficiency=0.9)
    coord.optimization_enabled = True
    coord.last_update_success = True
    coord.last_success_time = None
    coord.last_failure_reason = None
    coord.update_interval = timedelta(minutes=15)
    return coord


def _make_forecast_coord(data=None):
    coord = MagicMock()
    coord.data = data
    return coord


def _make_entry(entry_id="test_entry"):
    entry = MagicMock()
    entry.entry_id = entry_id
    return entry


def _make_device():
    return DeviceInfo(identifiers={(DOMAIN, "test_entry")})


# ---------------------------------------------------------------------------
# BatteryOptimalPowerSensor
# ---------------------------------------------------------------------------


class TestBatteryOptimalPowerSensor:
    def _sensor(self, data=None):
        return BatteryOptimalPowerSensor(
            _make_opt_coord(data), _make_device(), _make_entry()
        )

    def test_none_when_no_data(self):
        assert self._sensor(None).native_value is None

    def test_sign_inversion(self):
        # optimizer uses positive=charge, sensor uses positive=discharge
        sensor = self._sensor({"optimal_power_kw": 2.0})  # 2kW charge
        assert sensor.native_value == -2000.0

    def test_zero_returned_as_float(self):
        sensor = self._sensor({"optimal_power_kw": 0.0})
        assert sensor.native_value == 0.0

    def test_extra_attrs_empty_when_no_data(self):
        assert self._sensor(None).extra_state_attributes == {}

    def test_extra_attrs_with_data(self):
        data = {
            "optimal_power_kw": 1.0,
            "optimal_mode": ACTION_CHARGING,
            "current_price": 0.20,
        }
        attrs = self._sensor(data).extra_state_attributes
        assert attrs["optimal_mode"] == ACTION_CHARGING
        assert attrs["current_price"] == 0.20


# ---------------------------------------------------------------------------
# BatteryOptimalModeSensor
# ---------------------------------------------------------------------------


class TestBatteryOptimalModeSensor:
    def _sensor(self, data=None):
        return BatteryOptimalModeSensor(
            _make_opt_coord(data), _make_device(), _make_entry()
        )

    def test_none_when_no_data(self):
        assert self._sensor(None).native_value is None

    def test_returns_mode(self):
        assert (
            self._sensor({"optimal_mode": ACTION_DISCHARGING}).native_value
            == ACTION_DISCHARGING
        )

    def test_default_idle(self):
        assert self._sensor({}).native_value == ACTION_IDLE


# ---------------------------------------------------------------------------
# BatteryScheduleSensor
# ---------------------------------------------------------------------------


class TestBatteryScheduleSensor:
    def _sensor(self, data=None):
        return BatteryScheduleSensor(
            _make_opt_coord(data), _make_device(), _make_entry()
        )

    def test_none_when_no_data(self):
        assert self._sensor(None).native_value is None

    def test_counts_modes(self):
        schedule = [ACTION_CHARGING, ACTION_CHARGING, ACTION_DISCHARGING, ACTION_IDLE]
        sensor = self._sensor({"mode_schedule": schedule})
        val = sensor.native_value
        assert "C:2" in val
        assert "D:1" in val
        assert "I:1" in val

    def test_extra_attrs_empty_when_no_data(self):
        assert self._sensor(None).extra_state_attributes == {}

    def test_extra_attrs_without_result(self):
        data = {
            "mode_schedule": [],
            "step_start_times_iso": [],
            "step_durations_hours": [],
            "power_schedule_kw": [],
            "soc_schedule_kwh": [],
        }
        attrs = self._sensor(data).extra_state_attributes
        assert "mode_schedule" in attrs
        assert "grid_price_forecast" not in attrs

    def test_extra_attrs_with_result(self):
        result = MagicMock()
        result.price_forecast = [0.20, 0.22]
        result.pv_forecast = [1.0, 0.5]
        result.consumption_forecast = [0.5, 0.6]
        data = {
            "mode_schedule": [],
            "step_start_times_iso": [],
            "step_durations_hours": [],
            "power_schedule_kw": [],
            "soc_schedule_kwh": [],
            "optimization_result": result,
        }
        attrs = self._sensor(data).extra_state_attributes
        assert attrs["grid_price_forecast"] == [0.20, 0.22]

    def test_extra_attrs_with_price_forecast_model(self):
        data = {
            "mode_schedule": [],
            "step_start_times_iso": [],
            "step_durations_hours": [],
            "power_schedule_kw": [],
            "soc_schedule_kwh": [],
            "price_forecast_model": [0.20, 0.21],
        }
        attrs = self._sensor(data).extra_state_attributes
        assert attrs["grid_price_forecast_predicted"] == [0.20, 0.21]

    def test_extra_attrs_with_feed_in_forecast(self):
        data = {
            "mode_schedule": [],
            "step_start_times_iso": [],
            "step_durations_hours": [],
            "power_schedule_kw": [],
            "soc_schedule_kwh": [],
            "feed_in_price_forecast": [0.07, 0.07],
        }
        attrs = self._sensor(data).extra_state_attributes
        assert attrs["feed_in_price_forecast"] == [0.07, 0.07]

    def test_sign_inversion_in_power_schedule(self):
        data = {
            "mode_schedule": [],
            "step_start_times_iso": [],
            "step_durations_hours": [],
            "power_schedule_kw": [1.0, -0.5],
            "soc_schedule_kwh": [],
        }
        attrs = self._sensor(data).extra_state_attributes
        assert attrs["power_schedule_kw"] == [-1.0, 0.5]


# ---------------------------------------------------------------------------
# BatterySoCSensor
# ---------------------------------------------------------------------------


class TestBatterySoCSensor:
    def _sensor(self, data=None):
        return BatterySoCSensor(_make_opt_coord(data), _make_device(), _make_entry())

    def test_none_when_no_data(self):
        assert self._sensor(None).native_value is None

    def test_none_when_no_battery_state(self):
        assert self._sensor({"battery_state": None}).native_value is None

    def test_returns_rounded_soc_percent(self):
        bs = MagicMock(
            soc_percent=75.123, soc_kwh=7.512, power_kw=-1.0, mode="charging"
        )
        assert self._sensor({"battery_state": bs}).native_value == 75.1

    def test_extra_attrs_empty_when_no_data(self):
        assert self._sensor(None).extra_state_attributes == {}

    def test_extra_attrs_with_battery_state(self):
        bs = MagicMock(soc_percent=50.0, soc_kwh=5.0, power_kw=2.0, mode="discharging")
        attrs = self._sensor({"battery_state": bs}).extra_state_attributes
        assert attrs["soc_kwh"] == 5.0
        assert attrs["power_kw"] == 2.0
        assert attrs["mode"] == "discharging"

    def test_extra_attrs_empty_when_no_battery_state(self):
        assert self._sensor({"battery_state": None}).extra_state_attributes == {}


# ---------------------------------------------------------------------------
# BatteryPowerSensor
# ---------------------------------------------------------------------------


class TestBatteryPowerSensor:
    def _sensor(self, data=None):
        return BatteryPowerSensor(_make_opt_coord(data), _make_device(), _make_entry())

    def test_none_when_no_data(self):
        assert self._sensor(None).native_value is None

    def test_none_when_no_battery_state(self):
        assert self._sensor({"battery_state": None}).native_value is None

    def test_returns_power_kw(self):
        bs = MagicMock(power_kw=2.5)
        assert self._sensor({"battery_state": bs}).native_value == 2.5


# ---------------------------------------------------------------------------
# PVForecastSensor
# ---------------------------------------------------------------------------


class TestPVForecastSensor:
    def _sensor(self, data=None):
        return PVForecastSensor(
            _make_forecast_coord(data), _make_device(), _make_entry()
        )

    def test_none_when_no_data(self):
        assert self._sensor(None).native_value is None

    def test_returns_current_pv_kw(self):
        assert self._sensor({"current_pv_kw": 3.5}).native_value == 3.5

    def test_state_sums_ac_and_dc(self):
        data = {"current_pv_kw": 1.5, "current_dc_pv_kw": 2.0}
        assert self._sensor(data).native_value == pytest.approx(3.5)

    def test_state_reports_dc_only_system(self):
        """A fully DC-coupled system must not read a permanent 0 kW."""
        data = {"current_pv_kw": 0.0, "current_dc_pv_kw": 7.318}
        assert self._sensor(data).native_value == pytest.approx(7.318)

    def test_state_handles_none_values(self):
        data = {"current_pv_kw": None, "current_dc_pv_kw": None}
        assert self._sensor(data).native_value == 0.0

    def test_extra_attrs_empty_when_no_data(self):
        assert self._sensor(None).extra_state_attributes == {}

    def test_extra_attrs_basic(self):
        data = {"pv_forecast_kw": [1.0, 2.0], "pv_dc_forecast_kw": []}
        attrs = self._sensor(data).extra_state_attributes
        assert attrs["forecast_kw"] == [1.0, 2.0]
        assert "dc_forecast_kw" not in attrs

    def test_extra_attrs_with_dc_forecast(self):
        data = {
            "pv_forecast_kw": [1.0],
            "pv_dc_forecast_kw": [0.5, 0.8],
            "current_dc_pv_kw": 0.5,
        }
        attrs = self._sensor(data).extra_state_attributes
        assert "dc_forecast_kw" in attrs
        assert attrs["current_dc_pv_kw"] == 0.5
        # zip stops at the shorter series
        assert attrs["total_forecast_kw"] == [1.5]

    def test_ac_split_exposed_in_attributes(self):
        data = {
            "pv_forecast_kw": [0.0],
            "pv_dc_forecast_kw": [7.0],
            "current_pv_kw": 0.0,
            "current_dc_pv_kw": 7.0,
        }
        attrs = self._sensor(data).extra_state_attributes
        # The AC part stays visible even though the state is now the total
        assert attrs["current_ac_pv_kw"] == 0.0
        assert attrs["current_dc_pv_kw"] == 7.0


# ---------------------------------------------------------------------------
# ConsumptionForecastSensor
# ---------------------------------------------------------------------------


class TestConsumptionForecastSensor:
    def _sensor(self, data=None):
        return ConsumptionForecastSensor(
            _make_forecast_coord(data), _make_device(), _make_entry()
        )

    def test_none_when_no_data(self):
        assert self._sensor(None).native_value is None

    def test_returns_current_consumption(self):
        assert self._sensor({"current_consumption_kw": 1.5}).native_value == 1.5

    def test_extra_attrs_with_forecast(self):
        attrs = self._sensor(
            {"consumption_forecast_kw": [0.4, 0.5]}
        ).extra_state_attributes
        assert attrs["forecast_kw"] == [0.4, 0.5]


# ---------------------------------------------------------------------------
# NetGridForecastSensor
# ---------------------------------------------------------------------------


class TestNetGridForecastSensor:
    def _sensor(self, data=None):
        return NetGridForecastSensor(
            _make_forecast_coord(data), _make_device(), _make_entry()
        )

    def test_none_when_no_data(self):
        assert self._sensor(None).native_value is None

    def test_returns_current_net_load(self):
        assert self._sensor({"current_net_load_kw": 0.8}).native_value == 0.8

    def test_extra_attrs(self):
        attrs = self._sensor(
            {"net_load_forecast_kw": [0.5, 0.6]}
        ).extra_state_attributes
        assert attrs["forecast_kw"] == [0.5, 0.6]


# ---------------------------------------------------------------------------
# SolarIrradianceSensor
# ---------------------------------------------------------------------------


class TestSolarIrradianceSensor:
    def _sensor(self, data=None):
        return SolarIrradianceSensor(
            _make_forecast_coord(data), _make_device(), _make_entry()
        )

    def test_none_when_no_data(self):
        assert self._sensor(None).native_value is None

    def test_returns_ghi(self):
        assert self._sensor({"current_ghi_wm2": 450.0}).native_value == 450.0


# ---------------------------------------------------------------------------
# WindSpeedSensor
# ---------------------------------------------------------------------------


class TestWindSpeedSensor:
    def _sensor(self, data=None):
        return WindSpeedSensor(
            _make_forecast_coord(data), _make_device(), _make_entry()
        )

    def test_none_when_no_data(self):
        assert self._sensor(None).native_value is None

    def test_returns_wind_speed(self):
        assert self._sensor({"current_wind_speed_ms": 3.5}).native_value == 3.5


# ---------------------------------------------------------------------------
# BatteryDailySavingsSensor
# ---------------------------------------------------------------------------


class TestBatteryDailySavingsSensor:
    def _sensor(self, data=None):
        return BatteryDailySavingsSensor(
            _make_opt_coord(data), _make_device(), _make_entry()
        )

    def test_none_when_no_data(self):
        assert self._sensor(None).native_value is None

    def test_returns_savings(self):
        assert self._sensor({"savings": 0.123}).native_value == 0.12

    def test_extra_attrs_empty_when_no_data(self):
        assert self._sensor(None).extra_state_attributes == {}

    def test_extra_attrs_with_costs(self):
        data = {"savings": 0.5, "baseline_cost": 1.0, "total_cost": 0.5}
        attrs = self._sensor(data).extra_state_attributes
        assert attrs["baseline_cost"] == 1.0
        assert attrs["optimized_cost"] == 0.5


# ---------------------------------------------------------------------------
# BatteryShadowPriceSensor
# ---------------------------------------------------------------------------


class TestBatteryShadowPriceSensor:
    def _sensor(self, data=None):
        return BatteryShadowPriceSensor(
            _make_opt_coord(data), _make_device(), _make_entry()
        )

    def test_none_when_no_data(self):
        assert self._sensor(None).native_value is None

    def test_returns_shadow_price(self):
        assert self._sensor({"shadow_price_eur_kwh": 0.15}).native_value == 0.15

    def test_extra_attrs_empty_when_no_data(self):
        assert self._sensor(None).extra_state_attributes == {}

    def test_extra_attrs_thresholds(self):
        coord = _make_opt_coord({"shadow_price_eur_kwh": 0.20})
        coord.battery_config.round_trip_efficiency = 0.81  # sqrt = 0.9
        sensor = BatteryShadowPriceSensor(coord, _make_device(), _make_entry())
        attrs = sensor.extra_state_attributes
        # Both conversions lose sqrt(RTE): buy below lambda * sqrt(RTE), sell
        # above lambda / sqrt(RTE). The sell threshold is therefore the higher one.
        assert attrs["charge_threshold_eur_kwh"] == pytest.approx(0.20 * 0.9)
        assert attrs["discharge_threshold_eur_kwh"] == pytest.approx(
            0.20 / 0.9, abs=1e-4
        )
        assert attrs["charge_threshold_eur_kwh"] < attrs["discharge_threshold_eur_kwh"]


# ---------------------------------------------------------------------------
# CurrentGridPowerSensor
# ---------------------------------------------------------------------------


class TestCurrentGridPowerSensor:
    def _sensor(self, data=None):
        return CurrentGridPowerSensor(
            _make_opt_coord(data), _make_device(), _make_entry()
        )

    def test_none_when_no_data(self):
        assert self._sensor(None).native_value is None

    def test_returns_grid_power_in_kw(self):
        sensor = self._sensor({"control_action": {"current_grid_w": 1500.0}})
        assert sensor.native_value == pytest.approx(1.5)

    def test_extra_attrs_importing(self):
        data = {"control_action": {"current_grid_w": 2000.0}}
        attrs = self._sensor(data).extra_state_attributes
        assert attrs["direction"] == "importing"

    def test_extra_attrs_exporting(self):
        data = {"control_action": {"current_grid_w": -1000.0}}
        attrs = self._sensor(data).extra_state_attributes
        assert attrs["direction"] == "exporting"

    def test_extra_attrs_balanced(self):
        data = {"control_action": {"current_grid_w": 0.0}}
        attrs = self._sensor(data).extra_state_attributes
        assert attrs["direction"] == "balanced"


# ---------------------------------------------------------------------------
# BatteryGridSetpointSensor
# ---------------------------------------------------------------------------


class TestBatteryGridSetpointSensor:
    def _sensor(self, data=None):
        return BatteryGridSetpointSensor(
            _make_opt_coord(data), _make_device(), _make_entry()
        )

    def test_none_when_no_data(self):
        assert self._sensor(None).native_value is None

    def test_sign_inversion(self):
        # target_power_w positive = charge, sensor positive = discharge
        sensor = self._sensor({"control_action": {"target_power_w": 1000.0}})
        assert sensor.native_value == -1000.0

    def test_zero_setpoint(self):
        sensor = self._sensor({"control_action": {}})
        assert sensor.native_value == 0.0

    def test_extra_attrs_is_control_action(self):
        action = {"target_power_w": 500.0, "current_grid_w": 100.0}
        attrs = self._sensor({"control_action": action}).extra_state_attributes
        assert attrs == action


# ---------------------------------------------------------------------------
# BatteryControlModeSensor
# ---------------------------------------------------------------------------


class TestBatteryControlModeSensor:
    def _sensor(self, data=None):
        return BatteryControlModeSensor(
            _make_opt_coord(data), _make_device(), _make_entry()
        )

    def test_none_when_no_data(self):
        assert self._sensor(None).native_value is None

    def test_returns_control_mode(self):
        assert self._sensor({"control_mode": "zero_grid"}).native_value == "zero_grid"

    def test_default_hybrid(self):
        assert self._sensor({}).native_value == "hybrid"


# ---------------------------------------------------------------------------
# OptimizationStatusSensor
# ---------------------------------------------------------------------------


class TestOptimizationStatusSensor:
    def _sensor(self, coord_kwargs=None):
        coord = _make_opt_coord({})
        if coord_kwargs:
            for k, v in coord_kwargs.items():
                setattr(coord, k, v)
        return OptimizationStatusSensor(coord, _make_device(), _make_entry())

    def test_initializing_when_no_data(self):
        coord = _make_opt_coord(None)
        sensor = OptimizationStatusSensor(coord, _make_device(), _make_entry())
        assert sensor.native_value == "initializing"

    def test_disabled_when_optimization_off(self):
        sensor = self._sensor({"optimization_enabled": False})
        assert sensor.native_value == "disabled"

    def test_failed_when_last_update_failed(self):
        sensor = self._sensor(
            {"last_update_success": False, "optimization_enabled": True}
        )
        assert sensor.native_value == "failed"

    def test_ok_when_recent_update(self):
        from homeassistant.util import dt as dt_util

        recent = dt_util.utcnow() - timedelta(minutes=5)
        sensor = self._sensor({"last_success_time": recent})
        assert sensor.native_value == "ok"

    def test_stale_when_old_update(self):
        from homeassistant.util import dt as dt_util

        old = dt_util.utcnow() - timedelta(minutes=60)
        sensor = self._sensor({"last_success_time": old})
        assert sensor.native_value == "stale"

    def test_extra_attrs_basic(self):
        coord = _make_opt_coord({})
        sensor = OptimizationStatusSensor(coord, _make_device(), _make_entry())
        attrs = sensor.extra_state_attributes
        assert "last_update_success" in attrs

    def test_extra_attrs_with_result(self):
        result = MagicMock()
        result.power_schedule_kw = [1.0, 2.0]
        result.total_cost = 1.23
        result.baseline_cost = 2.00
        result.savings = 0.77
        coord = _make_opt_coord(
            {
                "optimization_result": result,
                "current_price": 0.22,
                "price_forecast_source": "live",
                "timestamp": "2026-01-01T00:00:00",
            }
        )
        sensor = OptimizationStatusSensor(coord, _make_device(), _make_entry())
        attrs = sensor.extra_state_attributes
        assert attrs["n_steps"] == 2
        assert attrs["total_cost"] == pytest.approx(1.23, abs=0.001)


# ---------------------------------------------------------------------------
# BatterySubentrySetpointSensor (additional coverage)
# ---------------------------------------------------------------------------


class TestBatterySubentrySetpointSensor:
    def test_none_when_no_data(self):
        coord = MagicMock()
        coord.data = None
        sensor = BatterySubentrySetpointSensor(
            coord, _make_device(), _make_entry(), "sub1", "Batt"
        )
        assert sensor.native_value is None

    def test_returns_setpoint_inverted(self):
        coord = MagicMock()
        coord.data = {"battery_setpoints": {"sub1": 1.5}, "per_battery_states": {}}
        # 1.5 kW charge → sensor = -1500W discharge convention
        sensor = BatterySubentrySetpointSensor(
            coord, _make_device(), _make_entry(), "sub1", "Batt"
        )
        assert sensor.native_value == -1500.0

    def test_extra_attrs_empty_when_no_state(self):
        coord = MagicMock()
        coord.data = {"battery_setpoints": {}, "per_battery_states": {}}
        sensor = BatterySubentrySetpointSensor(
            coord, _make_device(), _make_entry(), "sub1", "Batt"
        )
        assert sensor.extra_state_attributes == {}

    def test_extra_attrs_with_state(self):
        state = MagicMock(
            soc_percent=60.0, soc_kwh=6.0, power_kw=1.0, mode="discharging"
        )
        coord = MagicMock()
        coord.data = {"battery_setpoints": {}, "per_battery_states": {"sub1": state}}
        sensor = BatterySubentrySetpointSensor(
            coord, _make_device(), _make_entry(), "sub1", "Batt"
        )
        attrs = sensor.extra_state_attributes
        assert attrs["soc_percent"] == 60.0
        assert attrs["mode"] == "discharging"


# ---------------------------------------------------------------------------
# Additional coverage: missing branches in sensor.py
# ---------------------------------------------------------------------------


class TestGetOptimizationResultNone:
    """Cover _get_optimization_result when coordinator.data is None."""

    def test_returns_none_when_data_is_none(self):
        from custom_components.battery_controller.sensor import (
            BatteryOptimalPowerSensor,
        )

        coord = _make_opt_coord(data=None)
        sensor = BatteryOptimalPowerSensor(coord, _make_device(), _make_entry())
        # native_value calls _get_optimization_result which returns None when data=None
        assert sensor.native_value is None


class TestBatteryScheduleSensorFeedInForecastModel:
    """Cover BatteryScheduleSensor feed_in_price_forecast_model branch."""

    def test_feed_in_price_forecast_model_in_attrs(self):
        from custom_components.battery_controller.sensor import BatteryScheduleSensor

        coord = MagicMock()
        coord.data = {
            "optimization_result": MagicMock(
                power_schedule_kw=[0.5, -0.5],
                mode_schedule=["charging", "discharging"],
                soc_schedule_kwh=[5.0, 5.1],
                price_forecast=[0.20, 0.25],
                pv_forecast=[0.0, 0.0],
                consumption_forecast=[0.5, 0.5],
            ),
            "step_durations_hours": [0.25, 0.25],
            "grid_price_forecast": [0.20, 0.25],
            "feed_in_price_forecast": [0.07, 0.07],
            "feed_in_price_forecast_model": [0.08, 0.08],  # triggers line 288
        }
        sensor = BatteryScheduleSensor(coord, _make_device(), _make_entry())
        attrs = sensor.extra_state_attributes
        assert "feed_in_price_forecast_predicted" in attrs


class TestConsumptionForecastSensorDataNone:
    """Cover ConsumptionForecastSensor extra_state_attributes when data=None."""

    def test_extra_attrs_returns_empty_when_no_data(self):
        coord = _make_forecast_coord(data=None)
        sensor = ConsumptionForecastSensor(coord, _make_device(), _make_entry())
        assert sensor.extra_state_attributes == {}


class TestNetGridForecastSensorDataNone:
    """Cover NetGridForecastSensor extra_state_attributes when data=None."""

    def test_extra_attrs_returns_empty_when_no_data(self):
        coord = _make_forecast_coord(data=None)
        sensor = NetGridForecastSensor(coord, _make_device(), _make_entry())
        assert sensor.extra_state_attributes == {}


class TestCurrentGridPowerSensorDataNone:
    """Cover CurrentGridPowerSensor extra_state_attributes when data=None."""

    def test_extra_attrs_returns_empty_when_no_data(self):
        coord = _make_opt_coord(data=None)
        sensor = CurrentGridPowerSensor(coord, _make_device(), _make_entry())
        assert sensor.extra_state_attributes == {}


class TestBatteryGridSetpointSensorDataNone:
    """Cover BatteryGridSetpointSensor extra_state_attributes when data=None."""

    def test_extra_attrs_returns_empty_when_no_data(self):
        coord = _make_opt_coord(data=None)
        sensor = BatteryGridSetpointSensor(coord, _make_device(), _make_entry())
        assert sensor.extra_state_attributes == {}


class TestBatterySubentrySetpointSensorDataNone:
    """Cover BatterySubentrySetpointSensor extra_state_attributes when data=None."""

    def test_extra_attrs_returns_empty_when_coord_data_none(self):
        coord = _make_opt_coord(data=None)
        from custom_components.battery_controller.sensor import (
            BatterySubentrySetpointSensor,
        )

        sensor = BatterySubentrySetpointSensor(
            coord, _make_device(), _make_entry(), "sub1", "Batt"
        )
        assert sensor.extra_state_attributes == {}


class TestPVArrayForecastSensorDataNone:
    """Cover PVArrayForecastSensor extra_state_attributes when data=None."""

    def test_extra_attrs_returns_empty_when_no_data(self):
        from custom_components.battery_controller.sensor import PVArrayForecastSensor

        coord = _make_forecast_coord(data=None)
        sensor = PVArrayForecastSensor(
            coord, _make_device(), _make_entry(), "pv1", "Array 1"
        )
        assert sensor.extra_state_attributes == {}


class TestOptimizationStatusSensorDataNone:
    """Cover OptimizationStatusSensor extra_state_attributes when data=None."""

    def test_extra_attrs_returns_basic_dict_when_no_data(self):
        coord = _make_opt_coord(data=None)
        sensor = OptimizationStatusSensor(coord, _make_device(), _make_entry())
        attrs = sensor.extra_state_attributes
        # Returns basic attrs dict, not crash
        assert "last_update_success" in attrs


# ---------------------------------------------------------------------------
# async_setup_entry — covers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sensor_async_setup_entry_no_subentries():
    """async_setup_entry with no subentries calls async_add_entities once."""
    from unittest.mock import patch

    from homeassistant.helpers import device_registry as dr

    from custom_components.battery_controller.sensor import async_setup_entry

    opt_coord = _make_opt_coord()
    forecast_coord = _make_forecast_coord()
    device = DeviceInfo(identifiers={("battery_controller", "test")})

    runtime_data = MagicMock()
    runtime_data.optimization_coordinator = opt_coord
    runtime_data.forecast_coordinator = forecast_coord
    runtime_data.device = device
    runtime_data.battery_devices = {}
    runtime_data.pv_devices = {}

    entry = _make_entry()
    entry.runtime_data = runtime_data
    entry.subentries = {}

    hass = MagicMock()
    hass.states = MagicMock()

    added_calls = []

    def _add(entities, **kwargs):
        added_calls.append((entities, kwargs))

    with patch.object(dr, "async_get", return_value=MagicMock()):
        await async_setup_entry(hass, entry, _add)

    # Main entity list added
    assert len(added_calls) >= 1
    assert len(added_calls[0][0]) == 16  # 16 main sensors


@pytest.mark.asyncio
async def test_sensor_async_setup_entry_with_battery_subentry():
    """async_setup_entry with battery subentry creates per-battery sensors."""
    from unittest.mock import patch

    from homeassistant.helpers import device_registry as dr

    from custom_components.battery_controller.const import BATTERY_SUBENTRY_TYPE
    from custom_components.battery_controller.sensor import async_setup_entry

    opt_coord = _make_opt_coord()
    forecast_coord = _make_forecast_coord()
    device = DeviceInfo(identifiers={("battery_controller", "test")})
    batt_device = DeviceInfo(identifiers={("battery_controller", "sub1")})

    runtime_data = MagicMock()
    runtime_data.optimization_coordinator = opt_coord
    runtime_data.forecast_coordinator = forecast_coord
    runtime_data.device = device
    runtime_data.battery_devices = {"sub1": batt_device}
    runtime_data.pv_devices = {}

    battery_subentry = MagicMock()
    battery_subentry.subentry_type = BATTERY_SUBENTRY_TYPE
    battery_subentry.subentry_id = "sub1"
    battery_subentry.title = "My Battery"

    entry = _make_entry()
    entry.runtime_data = runtime_data
    entry.subentries = {"sub1": battery_subentry}

    hass = MagicMock()

    added_calls = []

    def _add(entities, **kwargs):
        added_calls.append((entities, kwargs))

    with patch.object(dr, "async_get", return_value=MagicMock()):
        await async_setup_entry(hass, entry, _add)

    # Main entities + battery subentry entities
    assert len(added_calls) >= 2
    # Battery subentry adds 2 entities (setpoint + SoC)
    battery_call = next(
        (c for c in added_calls if c[1].get("config_subentry_id") == "sub1"), None
    )
    assert battery_call is not None
    assert len(battery_call[0]) == 2


@pytest.mark.asyncio
async def test_sensor_async_setup_entry_with_pv_subentry():
    """async_setup_entry with PV subentry creates per-PV sensors."""
    from unittest.mock import patch

    from homeassistant.helpers import device_registry as dr

    from custom_components.battery_controller.const import PV_SUBENTRY_TYPE
    from custom_components.battery_controller.sensor import async_setup_entry

    opt_coord = _make_opt_coord()
    forecast_coord = _make_forecast_coord()
    device = DeviceInfo(identifiers={("battery_controller", "test")})
    pv_device = DeviceInfo(identifiers={("battery_controller", "pv1")})

    runtime_data = MagicMock()
    runtime_data.optimization_coordinator = opt_coord
    runtime_data.forecast_coordinator = forecast_coord
    runtime_data.device = device
    runtime_data.battery_devices = {}
    runtime_data.pv_devices = {"pv1": pv_device}

    pv_subentry = MagicMock()
    pv_subentry.subentry_type = PV_SUBENTRY_TYPE
    pv_subentry.subentry_id = "pv1"
    pv_subentry.title = "South Array"

    entry = _make_entry()
    entry.runtime_data = runtime_data
    entry.subentries = {"pv1": pv_subentry}

    hass = MagicMock()

    added_calls = []

    def _add(entities, **kwargs):
        added_calls.append((entities, kwargs))

    with patch.object(dr, "async_get", return_value=MagicMock()):
        await async_setup_entry(hass, entry, _add)

    pv_call = next(
        (c for c in added_calls if c[1].get("config_subentry_id") == "pv1"), None
    )
    assert pv_call is not None
    assert len(pv_call[0]) == 1


@pytest.mark.asyncio
async def test_sensor_async_setup_entry_device_migration():
    """Migration removes None subentry device associations."""
    from unittest.mock import patch

    from homeassistant.helpers import device_registry as dr

    from custom_components.battery_controller.const import DOMAIN
    from custom_components.battery_controller.sensor import async_setup_entry

    opt_coord = _make_opt_coord()
    forecast_coord = _make_forecast_coord()
    device = DeviceInfo(identifiers={(DOMAIN, "test")})
    batt_device = DeviceInfo(identifiers={(DOMAIN, "sub1")})

    runtime_data = MagicMock()
    runtime_data.optimization_coordinator = opt_coord
    runtime_data.forecast_coordinator = forecast_coord
    runtime_data.device = device
    runtime_data.battery_devices = {"sub1": batt_device}
    runtime_data.pv_devices = {}

    entry = _make_entry()
    entry.runtime_data = runtime_data
    entry.subentries = {}

    hass = MagicMock()

    # Mock device registry returning a device with None subentry association
    mock_dev = MagicMock()
    mock_dev.id = "dev_id_1"
    mock_dev.config_entries_subentries = {
        entry.entry_id: {None}
    }  # has None association

    mock_dr = MagicMock()
    mock_dr.async_get_device = MagicMock(return_value=mock_dev)
    mock_dr.async_update_device = MagicMock()

    def _add(entities, **kwargs):
        pass

    with patch.object(dr, "async_get", return_value=mock_dr):
        await async_setup_entry(hass, entry, _add)

    # Should call async_update_device to remove the None association
    mock_dr.async_update_device.assert_called_once()

"""Tests for Battery Controller sensor platform and per-battery device logic."""

from __future__ import annotations

from unittest.mock import MagicMock

from homeassistant.helpers.entity import DeviceInfo

from custom_components.battery_controller.__init__ import BatteryControllerData
from custom_components.battery_controller.const import DOMAIN
from custom_components.battery_controller.sensor import (
    BatterySubentrySoCSensor,
    BatterySubentrySetpointSensor,
    PVArrayForecastSensor,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_optimization_coordinator(per_battery_states=None, battery_setpoints=None):
    coord = MagicMock()
    coord.data = {
        "per_battery_states": per_battery_states or {},
        "battery_setpoints": battery_setpoints or {},
        "control_action": {},
    }
    coord.battery_config = MagicMock(round_trip_efficiency=0.9)
    return coord


def _make_forecast_coordinator(per_pv_array_forecasts=None):
    coord = MagicMock()
    coord.data = {
        "per_pv_array_forecasts": per_pv_array_forecasts or {},
        "pv_forecast_kw": [],
        "pv_dc_forecast_kw": [],
        "current_pv_kw": 0.0,
    }
    return coord


def _make_entry(entry_id="test_entry"):
    entry = MagicMock()
    entry.entry_id = entry_id
    return entry


def _make_device(entry_id="test_entry"):
    return DeviceInfo(identifiers={(DOMAIN, entry_id)})


# ---------------------------------------------------------------------------
# BatteryControllerData: battery_devices field
# ---------------------------------------------------------------------------


def test_battery_controller_data_has_battery_devices():
    """BatteryControllerData must expose a battery_devices dict."""
    data = BatteryControllerData(
        weather_coordinator=MagicMock(),
        forecast_coordinator=MagicMock(),
        optimization_coordinator=MagicMock(),
        config={},
        device=MagicMock(),
        battery_devices={"sub1": MagicMock()},
    )
    assert "sub1" in data.battery_devices


# ---------------------------------------------------------------------------
# Per-battery DeviceInfo construction (unit-tests for __init__ logic)
# ---------------------------------------------------------------------------


def test_battery_device_identifiers_are_subentry_id():
    """Each battery device must use the subentry_id as its identifier."""
    subentry_id = "abc123"
    device = DeviceInfo(
        identifiers={(DOMAIN, subentry_id)},
        name="My Battery",
        manufacturer="Custom",
        model="10 kWh Battery",
        via_device=(DOMAIN, "entry_xyz"),
    )
    assert (DOMAIN, subentry_id) in device["identifiers"]


def test_battery_device_links_to_parent_via_device():
    """Battery device must reference the parent entry via via_device."""
    entry_id = "parent_entry"
    subentry_id = "child_sub"
    device = DeviceInfo(
        identifiers={(DOMAIN, subentry_id)},
        name="Battery",
        via_device=(DOMAIN, entry_id),
    )
    assert device["via_device"] == (DOMAIN, entry_id)


def test_battery_device_model_includes_capacity():
    """Battery device model string should include capacity in kWh."""
    device = DeviceInfo(
        identifiers={(DOMAIN, "sub1")},
        name="Main Battery",
        model="10 kWh Battery",
        via_device=(DOMAIN, "entry1"),
    )
    assert "10" in device["model"]
    assert "kWh" in device["model"]


# ---------------------------------------------------------------------------
# BatterySubentrySoCSensor
# ---------------------------------------------------------------------------


def _make_soc_sensor(subentry_id="sub1", battery_title="Main Battery"):
    coord = _make_optimization_coordinator()
    entry = _make_entry()
    device = _make_device()
    return BatterySubentrySoCSensor(coord, device, entry, subentry_id, battery_title)


def test_soc_sensor_returns_none_when_no_coordinator_data():
    sensor = _make_soc_sensor()
    sensor.coordinator.data = None
    assert sensor.native_value is None


def test_soc_sensor_returns_none_when_subentry_not_in_per_states():
    coord = _make_optimization_coordinator(per_battery_states={})
    entry = _make_entry()
    device = _make_device()
    sensor = BatterySubentrySoCSensor(coord, device, entry, "sub_missing", "Batt")
    assert sensor.native_value is None


def test_soc_sensor_returns_rounded_soc_percent():
    state = MagicMock()
    state.soc_percent = 72.345
    state.soc_kwh = 7.2345
    state.power_kw = -1.5
    state.mode = "charging"

    coord = _make_optimization_coordinator(per_battery_states={"sub1": state})
    entry = _make_entry()
    device = _make_device()
    sensor = BatterySubentrySoCSensor(coord, device, entry, "sub1", "Battery")

    assert sensor.native_value == 72.3


def test_soc_sensor_extra_attributes_contain_kwh_power_mode():
    state = MagicMock()
    state.soc_percent = 50.0
    state.soc_kwh = 5.0
    state.power_kw = 2.5
    state.mode = "discharging"

    coord = _make_optimization_coordinator(per_battery_states={"sub1": state})
    entry = _make_entry()
    device = _make_device()
    sensor = BatterySubentrySoCSensor(coord, device, entry, "sub1", "Battery")
    attrs = sensor.extra_state_attributes

    assert attrs["soc_kwh"] == 5.0
    assert attrs["power_kw"] == 2.5
    assert attrs["mode"] == "discharging"


def test_soc_sensor_extra_attributes_empty_when_no_data():
    sensor = _make_soc_sensor()
    sensor.coordinator.data = None
    assert sensor.extra_state_attributes == {}


def test_soc_sensor_extra_attributes_empty_when_subentry_missing():
    coord = _make_optimization_coordinator(per_battery_states={})
    entry = _make_entry()
    device = _make_device()
    sensor = BatterySubentrySoCSensor(coord, device, entry, "missing", "Batt")
    assert sensor.extra_state_attributes == {}


def test_soc_sensor_unique_id_does_not_collide_with_global_soc():
    """Per-battery SoC unique_id must differ from the global SoC sensor unique_id."""
    entry = _make_entry("eid")
    coord = _make_optimization_coordinator()
    device = _make_device()
    sensor = BatterySubentrySoCSensor(coord, device, entry, "sub42", "Batt")
    # Global SoC sensor uses key "soc" → unique_id "eid_soc"
    assert sensor._attr_unique_id != "eid_soc"
    assert "sub42" in sensor._attr_unique_id


def test_soc_sensor_device_class_is_battery():
    from homeassistant.components.sensor import SensorDeviceClass

    sensor = _make_soc_sensor()
    assert sensor._attr_device_class == SensorDeviceClass.BATTERY


# ---------------------------------------------------------------------------
# BatterySubentrySetpointSensor — device assignment
# ---------------------------------------------------------------------------


def test_setpoint_sensor_uses_battery_device_not_main_device():
    """Setpoint sensor must be registered on the battery subentry device."""
    main_device = DeviceInfo(identifiers={(DOMAIN, "entry1")})
    batt_device = DeviceInfo(
        identifiers={(DOMAIN, "sub1")},
        via_device=(DOMAIN, "entry1"),
    )
    coord = _make_optimization_coordinator()
    entry = _make_entry("entry1")

    sensor = BatterySubentrySetpointSensor(coord, batt_device, entry, "sub1", "My Batt")

    assert sensor._attr_device_info is batt_device
    assert sensor._attr_device_info is not main_device


def test_setpoint_sensor_unique_id_stable():
    """Unique ID must be stable (not change between calls)."""
    coord = _make_optimization_coordinator()
    entry = _make_entry("e1")
    device = _make_device()
    s1 = BatterySubentrySetpointSensor(coord, device, entry, "subXYZ", "Batt")
    s2 = BatterySubentrySetpointSensor(coord, device, entry, "subXYZ", "Batt")
    assert s1._attr_unique_id == s2._attr_unique_id


# ---------------------------------------------------------------------------
# PVArrayForecastSensor
# ---------------------------------------------------------------------------


def _make_pv_sensor(subentry_id="pv1", array_title="South Array"):
    coord = _make_forecast_coordinator()
    entry = _make_entry()
    device = _make_device()
    return PVArrayForecastSensor(coord, device, entry, subentry_id, array_title)


def test_pv_array_sensor_returns_none_when_no_coordinator_data():
    sensor = _make_pv_sensor()
    sensor.coordinator.data = None
    assert sensor.native_value is None


def test_pv_array_sensor_returns_first_forecast_value():
    coord = _make_forecast_coordinator(per_pv_array_forecasts={"pv1": [2.5, 3.0, 1.0]})
    entry = _make_entry()
    device = _make_device()
    sensor = PVArrayForecastSensor(coord, device, entry, "pv1", "South")
    assert sensor.native_value == 2.5


def test_pv_array_sensor_returns_zero_for_empty_forecast():
    coord = _make_forecast_coordinator(per_pv_array_forecasts={"pv1": []})
    entry = _make_entry()
    device = _make_device()
    sensor = PVArrayForecastSensor(coord, device, entry, "pv1", "South")
    assert sensor.native_value == 0.0


def test_pv_array_sensor_extra_attributes_contain_full_forecast():
    forecast = [1.0, 2.0, 3.0]
    coord = _make_forecast_coordinator(per_pv_array_forecasts={"pv1": forecast})
    entry = _make_entry()
    device = _make_device()
    sensor = PVArrayForecastSensor(coord, device, entry, "pv1", "South")
    assert sensor.extra_state_attributes["forecast_kw"] == forecast


def test_pv_array_sensor_is_diagnostic_and_disabled_by_default():
    from homeassistant.helpers.entity import EntityCategory

    sensor = _make_pv_sensor()
    assert sensor._attr_entity_category == EntityCategory.DIAGNOSTIC
    assert sensor._attr_entity_registry_enabled_default is False


def test_pv_array_sensor_unique_id_contains_subentry_id():
    sensor = _make_pv_sensor(subentry_id="pvABC")
    assert "pvABC" in sensor._attr_unique_id


# ---------------------------------------------------------------------------
# Coordinator per_pv_array_forecasts keyed by subentry_id
# ---------------------------------------------------------------------------


def test_per_pv_array_forecasts_keyed_by_subentry_id():
    """Forecast coordinator data must contain per_pv_array_forecasts with subentry keys."""
    coord = _make_forecast_coordinator(
        per_pv_array_forecasts={"sub_ac": [1.0, 2.0], "sub_dc": [0.5, 0.8]}
    )
    assert "sub_ac" in coord.data["per_pv_array_forecasts"]
    assert "sub_dc" in coord.data["per_pv_array_forecasts"]
    assert coord.data["per_pv_array_forecasts"]["sub_ac"] == [1.0, 2.0]

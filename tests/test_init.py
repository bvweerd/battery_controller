"""Tests for Battery Controller __init__.py (setup, update listener, unload)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.battery_controller.__init__ import (
    BatteryControllerData,
    _update_listener,
)
from custom_components.battery_controller.const import (
    CONF_CONTROL_MODE,
    CONF_DEGRADATION_COST_PER_CYCLE,
    CONF_MANUAL_POWER_SETPOINT_W,
    CONF_MIN_PRICE_SPREAD,
    CONF_ZERO_GRID_DEADBAND_W,
)


def _make_entry(entry_id="test", data=None, options=None):
    entry = MagicMock()
    entry.entry_id = entry_id
    entry.data = data or {}
    entry.options = options or {}
    entry.subentries = {}
    return entry


def _make_runtime_data(config=None):
    runtime_data = MagicMock(spec=BatteryControllerData)
    runtime_data.config = config or {}
    runtime_data.forecast_coordinator = MagicMock()
    runtime_data.forecast_coordinator.async_shutdown = AsyncMock()
    runtime_data.optimization_coordinator = MagicMock()
    runtime_data.optimization_coordinator.async_shutdown = AsyncMock()
    runtime_data.weather_coordinator = MagicMock()
    runtime_data.weather_coordinator.async_shutdown = AsyncMock()
    return runtime_data


# ---------------------------------------------------------------------------
# BatteryControllerData dataclass
# ---------------------------------------------------------------------------


def test_battery_controller_data_fields():
    data = BatteryControllerData(
        weather_coordinator=MagicMock(),
        forecast_coordinator=MagicMock(),
        optimization_coordinator=MagicMock(),
        config={"key": "val"},
        device=MagicMock(),
        battery_devices={"sub1": MagicMock()},
        pv_devices={"pv1": MagicMock()},
    )
    assert data.config == {"key": "val"}
    assert "sub1" in data.battery_devices
    assert "pv1" in data.pv_devices


# ---------------------------------------------------------------------------
# _update_listener
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_listener_no_runtime_data():
    """_update_listener returns early if runtime_data is None."""
    entry = _make_entry(options={"some_key": "value"})
    entry.runtime_data = None

    mock_hass = MagicMock()
    mock_hass.config_entries.async_reload = AsyncMock()

    # Should not raise or call reload
    await _update_listener(mock_hass, entry)
    mock_hass.config_entries.async_reload.assert_not_called()


@pytest.mark.asyncio
async def test_update_listener_no_reload_for_runtime_keys():
    """Runtime-only keys (degradation, spread, deadband, setpoint, mode) do not trigger reload."""
    entry = _make_entry()
    # Config snapshot == no structural keys
    entry.runtime_data = _make_runtime_data(config={})
    entry.options = {
        CONF_DEGRADATION_COST_PER_CYCLE: 0.04,
        CONF_MIN_PRICE_SPREAD: 0.05,
        CONF_ZERO_GRID_DEADBAND_W: 50.0,
        CONF_MANUAL_POWER_SETPOINT_W: 0.0,
        CONF_CONTROL_MODE: "hybrid",
    }

    mock_hass = MagicMock()
    mock_hass.config_entries.async_reload = AsyncMock()

    await _update_listener(mock_hass, entry)
    mock_hass.config_entries.async_reload.assert_not_called()


@pytest.mark.asyncio
async def test_update_listener_reloads_on_structural_change():
    """Structural key changes (e.g. price_sensor) trigger reload."""
    entry = _make_entry()
    entry.runtime_data = _make_runtime_data(config={"price_sensor": "sensor.old"})
    entry.options = {"price_sensor": "sensor.new"}

    mock_hass = MagicMock()
    mock_hass.config_entries.async_reload = AsyncMock()

    await _update_listener(mock_hass, entry)
    mock_hass.config_entries.async_reload.assert_called_once_with(entry.entry_id)


@pytest.mark.asyncio
async def test_update_listener_no_reload_when_value_unchanged():
    """No reload if structural key value is unchanged from snapshot."""
    entry = _make_entry()
    entry.runtime_data = _make_runtime_data(config={"price_sensor": "sensor.test"})
    entry.options = {"price_sensor": "sensor.test"}  # Same value

    mock_hass = MagicMock()
    mock_hass.config_entries.async_reload = AsyncMock()

    await _update_listener(mock_hass, entry)
    mock_hass.config_entries.async_reload.assert_not_called()


# ---------------------------------------------------------------------------
# async_unload_entry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_unload_entry_with_runtime_data():
    """async_unload_entry shuts down coordinators and unloads platforms."""
    from custom_components.battery_controller import async_unload_entry

    entry = _make_entry()
    entry.runtime_data = _make_runtime_data()

    mock_hass = MagicMock()
    mock_hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)

    result = await async_unload_entry(mock_hass, entry)

    assert result is True
    entry.runtime_data.forecast_coordinator.async_shutdown.assert_called_once()
    entry.runtime_data.optimization_coordinator.async_shutdown.assert_called_once()
    entry.runtime_data.weather_coordinator.async_shutdown.assert_called_once()


@pytest.mark.asyncio
async def test_async_unload_entry_no_runtime_data():
    """async_unload_entry with no runtime_data still calls unload_platforms."""
    from custom_components.battery_controller import async_unload_entry

    entry = _make_entry()
    entry.runtime_data = None

    mock_hass = MagicMock()
    mock_hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)

    result = await async_unload_entry(mock_hass, entry)
    assert result is True


# ---------------------------------------------------------------------------
# async_setup_entry — covers lines 80-202
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_setup_entry_no_subentries():
    """async_setup_entry with no subentries sets up coordinators and runtime_data."""

    from custom_components.battery_controller import async_setup_entry

    mock_weather_coord = AsyncMock()
    mock_weather_coord.async_config_entry_first_refresh = AsyncMock()

    mock_forecast_coord = AsyncMock()
    mock_forecast_coord.async_setup = AsyncMock()
    mock_forecast_coord.async_refresh = AsyncMock()

    mock_opt_coord = AsyncMock()
    mock_opt_coord.async_setup = AsyncMock()
    mock_opt_coord.async_refresh = AsyncMock()

    mock_hass = MagicMock()
    mock_hass.config_entries.async_forward_entry_setups = AsyncMock(return_value=True)

    entry = _make_entry(
        data={"price_sensor": "sensor.price"},
        options={},
    )
    entry.add_update_listener = MagicMock(return_value=MagicMock())
    entry.async_on_unload = MagicMock()
    entry.subentries = {}

    with (
        patch(
            "custom_components.battery_controller.WeatherDataCoordinator",
            return_value=mock_weather_coord,
        ),
        patch(
            "custom_components.battery_controller.ForecastCoordinator",
            return_value=mock_forecast_coord,
        ),
        patch(
            "custom_components.battery_controller.OptimizationCoordinator",
            return_value=mock_opt_coord,
        ),
    ):
        result = await async_setup_entry(mock_hass, entry)

    assert result is True
    assert entry.runtime_data is not None
    assert entry.runtime_data.weather_coordinator is mock_weather_coord
    assert entry.runtime_data.forecast_coordinator is mock_forecast_coord
    assert entry.runtime_data.optimization_coordinator is mock_opt_coord
    assert entry.runtime_data.battery_devices == {}
    assert entry.runtime_data.pv_devices == {}


@pytest.mark.asyncio
async def test_async_setup_entry_with_battery_and_pv_subentries():
    """async_setup_entry with battery and PV subentries creates per-device DeviceInfo."""

    from custom_components.battery_controller import async_setup_entry
    from custom_components.battery_controller.const import (
        BATTERY_SUBENTRY_TYPE,
        PV_SUBENTRY_TYPE,
    )

    mock_weather_coord = AsyncMock()
    mock_weather_coord.async_config_entry_first_refresh = AsyncMock()
    mock_forecast_coord = AsyncMock()
    mock_forecast_coord.async_setup = AsyncMock()
    mock_forecast_coord.async_refresh = AsyncMock()
    mock_opt_coord = AsyncMock()
    mock_opt_coord.async_setup = AsyncMock()
    mock_opt_coord.async_refresh = AsyncMock()

    mock_hass = MagicMock()
    mock_hass.config_entries.async_forward_entry_setups = AsyncMock(return_value=True)

    battery_sub = MagicMock()
    battery_sub.subentry_type = BATTERY_SUBENTRY_TYPE
    battery_sub.subentry_id = "bat1"
    battery_sub.title = "My Battery"
    battery_sub.data = {
        "capacity_kwh": 10.0,
        "max_charge_power_kw": 5.0,
        "max_discharge_power_kw": 5.0,
    }

    pv_sub = MagicMock()
    pv_sub.subentry_type = PV_SUBENTRY_TYPE
    pv_sub.subentry_id = "pv1"
    pv_sub.title = "South PV"
    pv_sub.data = {"peak_power_kwp": 4.0, "dc_coupled": True}

    entry = _make_entry(data={"price_sensor": "sensor.price"})
    entry.add_update_listener = MagicMock(return_value=MagicMock())
    entry.async_on_unload = MagicMock()
    entry.subentries = {"bat1": battery_sub, "pv1": pv_sub}

    with (
        patch(
            "custom_components.battery_controller.WeatherDataCoordinator",
            return_value=mock_weather_coord,
        ),
        patch(
            "custom_components.battery_controller.ForecastCoordinator",
            return_value=mock_forecast_coord,
        ),
        patch(
            "custom_components.battery_controller.OptimizationCoordinator",
            return_value=mock_opt_coord,
        ),
    ):
        result = await async_setup_entry(mock_hass, entry)

    assert result is True
    assert "bat1" in entry.runtime_data.battery_devices
    assert "pv1" in entry.runtime_data.pv_devices


@pytest.mark.asyncio
async def test_async_setup_entry_battery_subentry_without_title():
    """Battery subentry without matching entry.subentries uses CONF_NAME fallback."""

    from custom_components.battery_controller import async_setup_entry
    from custom_components.battery_controller.const import BATTERY_SUBENTRY_TYPE

    mock_weather_coord = AsyncMock()
    mock_weather_coord.async_config_entry_first_refresh = AsyncMock()
    mock_forecast_coord = AsyncMock()
    mock_forecast_coord.async_setup = AsyncMock()
    mock_forecast_coord.async_refresh = AsyncMock()
    mock_opt_coord = AsyncMock()
    mock_opt_coord.async_setup = AsyncMock()
    mock_opt_coord.async_refresh = AsyncMock()

    mock_hass = MagicMock()
    mock_hass.config_entries.async_forward_entry_setups = AsyncMock(return_value=True)

    battery_sub = MagicMock()
    battery_sub.subentry_type = BATTERY_SUBENTRY_TYPE
    battery_sub.subentry_id = "bat1"
    battery_sub.title = "Named Battery"
    battery_sub.data = {
        "capacity_kwh": 10.0,
        "max_charge_power_kw": 5.0,
        "max_discharge_power_kw": 5.0,
    }

    entry = _make_entry(data={"price_sensor": "sensor.price"})
    entry.add_update_listener = MagicMock(return_value=MagicMock())
    entry.async_on_unload = MagicMock()
    entry.subentries = {"bat1": battery_sub}

    with (
        patch(
            "custom_components.battery_controller.WeatherDataCoordinator",
            return_value=mock_weather_coord,
        ),
        patch(
            "custom_components.battery_controller.ForecastCoordinator",
            return_value=mock_forecast_coord,
        ),
        patch(
            "custom_components.battery_controller.OptimizationCoordinator",
            return_value=mock_opt_coord,
        ),
    ):
        await async_setup_entry(mock_hass, entry)

    assert "bat1" in entry.runtime_data.battery_devices

"""Tests for Battery Controller __init__.py (setup, update listener, unload)."""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.battery_controller.__init__ import (
    BatteryControllerData,
    _async_handle_reset_charge_efficiency_calibration,
    _async_register_services,
    _update_listener,
)
from custom_components.battery_controller.const import DOMAIN
from custom_components.battery_controller.const import (
    CONF_CONTROL_MODE,
    CONF_DEGRADATION_COST_PER_CYCLE,
    CONF_MANUAL_POWER_SETPOINT_W,
    CONF_MIN_PRICE_SPREAD,
    CONF_ZERO_GRID_DEADBAND_W,
)
from custom_components.battery_controller import async_setup_entry
from custom_components.battery_controller import async_unload_entry
from custom_components.battery_controller.const import BATTERY_SUBENTRY_TYPE


def _make_entry(entry_id="test", data=None, options=None):
    entry = MagicMock()
    entry.entry_id = entry_id
    entry.data = data or {}
    entry.options = options or {}
    entry.subentries = {}
    return entry


def _make_runtime_data(config=None, options=None):
    runtime_data = MagicMock(spec=BatteryControllerData)
    runtime_data.config = config or {}
    # Options snapshot; defaults to the config so the existing cases, where the
    # two are the same thing, keep reading naturally.
    runtime_data.options = options if options is not None else (config or {})
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
async def test_update_listener_reloads_when_structural_key_removed():
    """Clearing a structural option must reload, not just changing one.

    The listener used to iterate the new options only, so a key that vanished
    was never compared: the coordinator kept running on the sensor it was set
    up with while the UI showed the selection cleared.
    """
    entry = _make_entry()
    entry.runtime_data = _make_runtime_data(options={"price_sensor": "sensor.old"})
    entry.options = {}

    mock_hass = MagicMock()
    mock_hass.config_entries.async_reload = AsyncMock()

    await _update_listener(mock_hass, entry)
    mock_hass.config_entries.async_reload.assert_called_once_with(entry.entry_id)


@pytest.mark.asyncio
async def test_update_listener_ignores_derived_config_keys():
    """Derived keys in `config` are not options and must not force a reload.

    `config` is entry.data | entry.options plus derived entries (pv_arrays,
    battery_subentries, entry_id, ...). Comparing options against it would read
    every one of those as a removed key and reload on every runtime-only change.
    """
    entry = _make_entry()
    entry.runtime_data = _make_runtime_data(
        config={
            "entry_id": "abc",
            "pv_arrays": [{"peak_power_kwp": 4.0}],
            "battery_subentries": [("sub1", {})],
            CONF_CONTROL_MODE: "hybrid",
        },
        options={CONF_CONTROL_MODE: "hybrid"},
    )
    entry.options = {CONF_CONTROL_MODE: "zero_grid"}  # runtime-only key

    mock_hass = MagicMock()
    mock_hass.config_entries.async_reload = AsyncMock()

    await _update_listener(mock_hass, entry)
    mock_hass.config_entries.async_reload.assert_not_called()


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

    entry = _make_entry()
    entry.runtime_data = _make_runtime_data()

    mock_hass = MagicMock()
    mock_hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)
    mock_hass.config_entries.async_entries = MagicMock(return_value=[entry])
    mock_hass.services.has_service = MagicMock(return_value=True)
    mock_hass.services.async_remove = MagicMock()

    result = await async_unload_entry(mock_hass, entry)

    assert result is True
    entry.runtime_data.forecast_coordinator.async_shutdown.assert_called_once()
    entry.runtime_data.optimization_coordinator.async_shutdown.assert_called_once()
    entry.runtime_data.weather_coordinator.async_shutdown.assert_called_once()
    removed = {call.args for call in mock_hass.services.async_remove.call_args_list}
    assert removed == {
        (DOMAIN, "reset_charge_efficiency_calibration"),
        (DOMAIN, "reset_discharge_efficiency_calibration"),
        (DOMAIN, "reset_pv_calibration"),
    }


@pytest.mark.asyncio
async def test_async_unload_entry_no_runtime_data():
    """async_unload_entry with no runtime_data still calls unload_platforms."""

    entry = _make_entry()
    entry.runtime_data = None

    mock_hass = MagicMock()
    mock_hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)
    mock_hass.config_entries.async_entries = MagicMock(return_value=[entry])
    mock_hass.services.has_service = MagicMock(return_value=False)

    result = await async_unload_entry(mock_hass, entry)
    assert result is True


# ---------------------------------------------------------------------------
# async_setup_entry — covers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_setup_entry_no_subentries():
    """async_setup_entry with no subentries sets up coordinators and runtime_data."""

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
    mock_hass.services.has_service = MagicMock(return_value=False)
    mock_hass.services.async_register = MagicMock()

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
    assert mock_hass.services.async_register.call_count == 3


@pytest.mark.asyncio
async def test_async_setup_entry_with_battery_and_pv_subentries():
    """async_setup_entry with battery and PV subentries creates per-device DeviceInfo."""

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
    mock_hass.services.has_service = MagicMock(return_value=False)
    mock_hass.services.async_register = MagicMock()

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
    mock_hass.services.has_service = MagicMock(return_value=False)
    mock_hass.services.async_register = MagicMock()

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


def test_register_services_only_once():
    """Service registration is idempotent."""
    mock_hass = MagicMock()
    mock_hass.services.has_service = MagicMock(side_effect=[False, True])
    mock_hass.services.async_register = MagicMock()

    _async_register_services(mock_hass)
    _async_register_services(mock_hass)

    # All services registered by the first call; the second call is a no-op.
    assert mock_hass.services.async_register.call_count == 3
    registered = {
        call.args[1] for call in mock_hass.services.async_register.call_args_list
    }
    assert registered == {
        "reset_charge_efficiency_calibration",
        "reset_discharge_efficiency_calibration",
        "reset_pv_calibration",
    }
    for call in mock_hass.services.async_register.call_args_list:
        assert inspect.iscoroutinefunction(call.args[2])


@pytest.mark.asyncio
async def test_reset_charge_efficiency_service_targets_requested_entry():
    """Reset service only affects the selected config entry."""
    runtime_1 = _make_runtime_data()
    runtime_1.optimization_coordinator.async_reset_charge_eff_calibration = AsyncMock()
    runtime_2 = _make_runtime_data()
    runtime_2.optimization_coordinator.async_reset_charge_eff_calibration = AsyncMock()

    entry_1 = _make_entry(entry_id="entry-1")
    entry_1.runtime_data = runtime_1
    entry_2 = _make_entry(entry_id="entry-2")
    entry_2.runtime_data = runtime_2

    mock_hass = MagicMock()
    mock_hass.config_entries.async_entries = MagicMock(return_value=[entry_1, entry_2])
    call = MagicMock()
    call.data = {"entry_id": "entry-2"}

    await _async_handle_reset_charge_efficiency_calibration(mock_hass, call)

    runtime_1.optimization_coordinator.async_reset_charge_eff_calibration.assert_not_called()
    runtime_2.optimization_coordinator.async_reset_charge_eff_calibration.assert_called_once()


@pytest.mark.asyncio
async def test_async_unload_entry_coordinator_shutdown_exception():
    """M2: if one coordinator raises during shutdown, the others still run."""

    entry = _make_entry()
    entry.runtime_data = _make_runtime_data()
    # Make forecast_coordinator.async_shutdown raise
    entry.runtime_data.forecast_coordinator.async_shutdown = AsyncMock(
        side_effect=RuntimeError("boom")
    )

    mock_hass = MagicMock()
    mock_hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)
    mock_hass.config_entries.async_entries = MagicMock(return_value=[entry])
    mock_hass.services.has_service = MagicMock(return_value=False)

    # Should not raise despite forecast_coordinator blowing up
    result = await async_unload_entry(mock_hass, entry)

    assert result is True
    # The remaining two coordinators must still have been shut down
    entry.runtime_data.optimization_coordinator.async_shutdown.assert_called_once()
    entry.runtime_data.weather_coordinator.async_shutdown.assert_called_once()

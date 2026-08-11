"""Tests for ForecastCoordinator."""

from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


from custom_components.battery_controller.const import (
    CONF_BATTERY_ENERGY_CHARGED_SENSOR,
    CONF_PV_MEASURED_PRODUCTION_SENSOR,
)
from custom_components.battery_controller.coordinator_forecast import (
    ForecastCoordinator,
    _battery_energy_sensors,
)


def _minimal_config(pv_arrays=None, battery_subentries=None):
    return {
        "entry_id": "test-entry",
        "pv_arrays": pv_arrays or [],
        "battery_subentries": battery_subentries or [],
        "electricity_consumption_sensors": [],
        "electricity_production_sensors": [],
        "pv_production_sensors": [],
    }


def _make_mock_consumption():
    mock = MagicMock()
    mock.async_update_pattern = AsyncMock()
    mock.get_current_consumption = MagicMock(return_value=0.5)
    return mock


@pytest.mark.asyncio
async def test_forecast_coordinator_init_no_pv(hass):
    """ForecastCoordinator initializes with no PV models when no arrays configured."""
    weather_coord = MagicMock()
    weather_coord.data = None
    config = _minimal_config()

    with patch(
        "custom_components.battery_controller.coordinator_forecast.ConsumptionForecastModel",
        return_value=_make_mock_consumption(),
    ):
        coord = ForecastCoordinator(hass, weather_coord, config)

    assert coord.pv_ac_models == []
    assert coord.pv_dc_models == []


@pytest.mark.asyncio
async def test_forecast_coordinator_init_with_ac_pv(hass):
    """ForecastCoordinator builds AC PV model from subentry data."""
    pv_arrays = [
        {
            "subentry_id": "pv_ac_1",
            "peak_power_kwp": 4.0,
            "orientation": 180.0,
            "tilt": 35.0,
            "efficiency_factor": 0.85,
            "dc_coupled": False,
        }
    ]
    config = _minimal_config(pv_arrays=pv_arrays)
    weather_coord = MagicMock()

    with patch(
        "custom_components.battery_controller.coordinator_forecast.ConsumptionForecastModel",
        return_value=_make_mock_consumption(),
    ):
        coord = ForecastCoordinator(hass, weather_coord, config)

    assert len(coord.pv_ac_models) == 1
    assert len(coord.pv_dc_models) == 0
    assert coord.pv_ac_subentry_ids == ["pv_ac_1"]


@pytest.mark.asyncio
async def test_forecast_coordinator_init_with_dc_pv(hass):
    """ForecastCoordinator builds DC PV model from subentry data."""
    pv_arrays = [
        {
            "subentry_id": "pv_dc_1",
            "peak_power_kwp": 3.0,
            "orientation": 180.0,
            "tilt": 35.0,
            "efficiency_factor": 0.97,
            "dc_coupled": True,
        }
    ]
    config = _minimal_config(pv_arrays=pv_arrays)
    weather_coord = MagicMock()

    with patch(
        "custom_components.battery_controller.coordinator_forecast.ConsumptionForecastModel",
        return_value=_make_mock_consumption(),
    ):
        coord = ForecastCoordinator(hass, weather_coord, config)

    assert len(coord.pv_dc_models) == 1
    assert len(coord.pv_ac_models) == 0


@pytest.mark.asyncio
async def test_forecast_coordinator_init_zero_kwp_skipped(hass):
    """PV arrays with 0 kWp are skipped."""
    pv_arrays = [
        {
            "subentry_id": "pv_zero",
            "peak_power_kwp": 0.0,
            "dc_coupled": False,
        }
    ]
    config = _minimal_config(pv_arrays=pv_arrays)
    weather_coord = MagicMock()

    with patch(
        "custom_components.battery_controller.coordinator_forecast.ConsumptionForecastModel",
        return_value=_make_mock_consumption(),
    ):
        coord = ForecastCoordinator(hass, weather_coord, config)

    assert coord.pv_ac_models == []
    assert coord.pv_dc_models == []


@pytest.mark.asyncio
async def test_async_setup_registers_pattern_refresh(hass):
    """async_setup calls consumption pattern update and registers timer."""
    config = _minimal_config()
    weather_coord = MagicMock()

    mock_unsub = MagicMock()
    mock_consumption = _make_mock_consumption()

    with (
        patch(
            "custom_components.battery_controller.coordinator_forecast.ConsumptionForecastModel",
            return_value=mock_consumption,
        ),
        patch(
            "custom_components.battery_controller.coordinator_forecast.async_track_time_interval",
            return_value=mock_unsub,
        ) as mock_track,
    ):
        coord = ForecastCoordinator(hass, weather_coord, config)
        await coord.async_setup()

    mock_consumption.async_update_pattern.assert_called_once()
    mock_track.assert_called_once()
    assert coord._unsub_pattern_refresh is mock_unsub


@pytest.mark.asyncio
async def test_async_shutdown_with_unsub(hass):
    """async_shutdown calls the unsub function."""
    config = _minimal_config()
    weather_coord = MagicMock()

    with patch(
        "custom_components.battery_controller.coordinator_forecast.ConsumptionForecastModel",
        return_value=_make_mock_consumption(),
    ):
        coord = ForecastCoordinator(hass, weather_coord, config)

    mock_unsub = MagicMock()
    coord._unsub_pattern_refresh = mock_unsub

    await coord.async_shutdown()

    mock_unsub.assert_called_once()
    assert coord._unsub_pattern_refresh is None


@pytest.mark.asyncio
async def test_async_shutdown_without_unsub(hass):
    """async_shutdown is safe when no timer is registered."""
    config = _minimal_config()
    weather_coord = MagicMock()

    with patch(
        "custom_components.battery_controller.coordinator_forecast.ConsumptionForecastModel",
        return_value=_make_mock_consumption(),
    ):
        coord = ForecastCoordinator(hass, weather_coord, config)

    coord._unsub_pattern_refresh = None

    await coord.async_shutdown()

    # Nothing was registered, so nothing had to be released.
    assert coord._unsub_pattern_refresh is None
    assert coord._unsub_weather is None


@pytest.mark.asyncio
async def test_async_update_data_no_weather_data(hass):
    """_async_update_data falls back to zero radiation when weather data unavailable."""
    config = _minimal_config()
    weather_coord = MagicMock()
    weather_coord.data = None  # No weather data

    mock_consumption = _make_mock_consumption()

    with patch(
        "custom_components.battery_controller.coordinator_forecast.ConsumptionForecastModel",
        return_value=mock_consumption,
    ):
        coord = ForecastCoordinator(hass, weather_coord, config)

    # Mock net_load_model to return realistic data (same pattern as other tests)
    coord.net_load_model = MagicMock()
    coord.net_load_model.forecast = MagicMock(return_value=(None, [0.5] * 48, None))

    result = await coord._async_update_data()

    # Should succeed with zero PV forecast at 15-min resolution: 48 hours of
    # fallback radiation minus the steps already elapsed in the current hour.
    assert result is not None
    n = len(result["pv_forecast_kw"])
    assert 48 * 4 - 3 <= n <= 48 * 4
    assert result["pv_forecast_kw"] == [0.0] * n
    assert result["consumption_forecast_kw"] == [0.5] * n
    assert result["forecast_interval_minutes"] == 15


@pytest.mark.asyncio
async def test_async_update_data_with_weather_data(hass):
    """_async_update_data returns forecast dict when weather data available."""
    config = _minimal_config()

    now = datetime(2024, 6, 15, 10, 0, 0, tzinfo=timezone.utc)
    weather_data = {
        "radiation_forecast": [float(100 + i) for i in range(24)],
        "dni_forecast": [float(50 + i) for i in range(24)],
        "diffuse_forecast": [float(20 + i) for i in range(24)],
        "wind_speed_forecast": [float(3.0) for _ in range(24)],
        "temperature_forecast": [float(15.0) for _ in range(24)],
        "forecast_start_utc": now,
    }
    weather_coord = MagicMock()
    weather_coord.data = weather_data

    mock_consumption = _make_mock_consumption()

    with (
        patch(
            "custom_components.battery_controller.coordinator_forecast.ConsumptionForecastModel",
            return_value=mock_consumption,
        ),
        patch(
            "custom_components.battery_controller.coordinator_forecast.dt_util.utcnow",
            return_value=now,
        ),
        patch(
            "custom_components.battery_controller.coordinator_forecast.dt_util.now",
            return_value=now,
        ),
    ):
        coord = ForecastCoordinator(hass, weather_coord, config)

        # net_load_model.forecast returns (_, consumption, _)
        coord.net_load_model = MagicMock()
        coord.net_load_model.forecast = MagicMock(return_value=(None, [0.5] * 24, None))

        result = await coord._async_update_data()

    assert "pv_forecast_kw" in result
    assert "consumption_forecast_kw" in result
    assert "current_pv_kw" in result
    assert "current_consumption_kw" in result


@pytest.mark.asyncio
async def test_handle_pattern_refresh(hass):
    """_handle_pattern_refresh calls consumption model pattern update."""
    config = _minimal_config()
    weather_coord = MagicMock()

    mock_consumption = _make_mock_consumption()

    with patch(
        "custom_components.battery_controller.coordinator_forecast.ConsumptionForecastModel",
        return_value=mock_consumption,
    ):
        coord = ForecastCoordinator(hass, weather_coord, config)

    mock_consumption.async_update_pattern.reset_mock()
    await coord._handle_pattern_refresh(datetime.now())
    mock_consumption.async_update_pattern.assert_called_once()


@pytest.mark.asyncio
async def test_async_update_data_time_shifted_forecast(hass):
    """When weather data is older than 0 hours, forecasts are shifted."""
    config = _minimal_config()

    # Weather data from 2 hours ago — hours_elapsed = 2
    forecast_start = datetime(2024, 6, 15, 8, 0, 0, tzinfo=timezone.utc)
    now = datetime(2024, 6, 15, 10, 0, 0, tzinfo=timezone.utc)  # 2 hours later

    radiation_full = [float(100 + i) for i in range(48)]
    weather_data = {
        "radiation_forecast": radiation_full,
        "dni_forecast": [float(50 + i) for i in range(48)],
        "diffuse_forecast": [float(20 + i) for i in range(48)],
        "wind_speed_forecast": [float(3.0) for _ in range(48)],
        "temperature_forecast": [float(15.0) for _ in range(48)],
        "forecast_start_utc": forecast_start,
    }
    weather_coord = MagicMock()
    weather_coord.data = weather_data

    mock_consumption = _make_mock_consumption()

    with (
        patch(
            "custom_components.battery_controller.coordinator_forecast.ConsumptionForecastModel",
            return_value=mock_consumption,
        ),
        patch(
            "custom_components.battery_controller.coordinator_forecast.dt_util.utcnow",
            return_value=now,
        ),
        patch(
            "custom_components.battery_controller.coordinator_forecast.dt_util.now",
            return_value=now,
        ),
    ):
        coord = ForecastCoordinator(hass, weather_coord, config)
        coord.net_load_model = MagicMock()
        # Simulate that forecast() returns based on shifted radiation (46 hours left)
        coord.net_load_model.forecast = MagicMock(return_value=(None, [0.5] * 46, None))

        result = await coord._async_update_data()

    # Forecast was shifted by 2 hours, so 48-2=46 hours remain (at 15-min steps)
    assert "pv_forecast_kw" in result
    assert len(result["pv_forecast_kw"]) == 46 * 4


@pytest.mark.asyncio
async def test_async_update_data_with_ac_pv_model(hass):
    """ForecastCoordinator processes AC PV arrays in _async_update_data."""
    pv_arrays = [
        {
            "subentry_id": "pv_ac_1",
            "peak_power_kwp": 4.0,
            "orientation": 180.0,
            "tilt": 35.0,
            "efficiency_factor": 0.85,
            "dc_coupled": False,
        }
    ]
    config = _minimal_config(pv_arrays=pv_arrays)

    now = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
    weather_data = {
        "radiation_forecast": [float(100 + i) for i in range(24)],
        "dni_forecast": [float(50 + i) for i in range(24)],
        "diffuse_forecast": [float(20 + i) for i in range(24)],
        "wind_speed_forecast": [3.0] * 24,
        "temperature_forecast": [20.0] * 24,
        "forecast_start_utc": now,
    }
    weather_coord = MagicMock()
    weather_coord.data = weather_data

    mock_consumption = _make_mock_consumption()

    with (
        patch(
            "custom_components.battery_controller.coordinator_forecast.ConsumptionForecastModel",
            return_value=mock_consumption,
        ),
        patch(
            "custom_components.battery_controller.coordinator_forecast.dt_util.utcnow",
            return_value=now,
        ),
        patch(
            "custom_components.battery_controller.coordinator_forecast.dt_util.now",
            return_value=now,
        ),
    ):
        coord = ForecastCoordinator(hass, weather_coord, config)
        coord.net_load_model = MagicMock()
        coord.net_load_model.forecast = MagicMock(return_value=(None, [0.5] * 24, None))

        result = await coord._async_update_data()

    assert "pv_forecast_kw" in result
    # Should have per_pv_array_forecasts for the AC array
    assert "pv_ac_1" in result.get("per_pv_array_forecasts", {})


@pytest.mark.asyncio
async def test_async_update_data_with_dc_pv_model(hass):
    """ForecastCoordinator processes DC PV arrays in _async_update_data."""
    pv_arrays = [
        {
            "subentry_id": "pv_dc_1",
            "peak_power_kwp": 3.0,
            "orientation": 180.0,
            "tilt": 35.0,
            "efficiency_factor": 0.97,
            "dc_coupled": True,
        }
    ]
    config = _minimal_config(pv_arrays=pv_arrays)

    now = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
    weather_data = {
        "radiation_forecast": [float(100 + i) for i in range(24)],
        "dni_forecast": [float(50 + i) for i in range(24)],
        "diffuse_forecast": [float(20 + i) for i in range(24)],
        "wind_speed_forecast": [3.0] * 24,
        "temperature_forecast": [20.0] * 24,
        "forecast_start_utc": now,
    }
    weather_coord = MagicMock()
    weather_coord.data = weather_data

    mock_consumption = _make_mock_consumption()

    with (
        patch(
            "custom_components.battery_controller.coordinator_forecast.ConsumptionForecastModel",
            return_value=mock_consumption,
        ),
        patch(
            "custom_components.battery_controller.coordinator_forecast.dt_util.utcnow",
            return_value=now,
        ),
        patch(
            "custom_components.battery_controller.coordinator_forecast.dt_util.now",
            return_value=now,
        ),
    ):
        coord = ForecastCoordinator(hass, weather_coord, config)
        coord.net_load_model = MagicMock()
        coord.net_load_model.forecast = MagicMock(return_value=(None, [0.5] * 24, None))

        result = await coord._async_update_data()

    assert "pv_dc_forecast_kw" in result
    assert "pv_dc_1" in result.get("per_pv_array_forecasts", {})


@pytest.mark.asyncio
async def test_async_update_data_missing_weather_creates_issue(hass):
    """Missing weather data should create the weather-data-unavailable issue."""
    config = _minimal_config()
    weather_coord = MagicMock()
    weather_coord.data = None
    mock_consumption = _make_mock_consumption()

    with (
        patch(
            "custom_components.battery_controller.coordinator_forecast.ConsumptionForecastModel",
            return_value=mock_consumption,
        ),
        patch(
            "custom_components.battery_controller.coordinator_forecast.ir.async_create_issue"
        ) as mock_create_issue,
    ):
        coord = ForecastCoordinator(hass, weather_coord, config)
        coord.net_load_model = MagicMock()
        coord.net_load_model.forecast = MagicMock(return_value=(None, [0.5] * 48, None))

        result = await coord._async_update_data()

    assert result["pv_forecast_kw"] == [0.0] * len(result["pv_forecast_kw"])
    assert len(result["pv_forecast_kw"]) >= 48 * 4 - 3
    mock_create_issue.assert_called_once()


@pytest.mark.asyncio
async def test_async_update_data_weather_recovery_deletes_issue(hass):
    """Available weather data should delete the weather-data-unavailable issue."""
    now = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
    weather_coord = MagicMock()
    weather_coord.data = {
        "radiation_forecast": [100.0] * 4,
        "dni_forecast": [50.0] * 4,
        "diffuse_forecast": [20.0] * 4,
        "wind_speed_forecast": [3.0] * 4,
        "temperature_forecast": [20.0] * 4,
        "forecast_start_utc": now,
    }
    mock_consumption = _make_mock_consumption()

    with (
        patch(
            "custom_components.battery_controller.coordinator_forecast.ConsumptionForecastModel",
            return_value=mock_consumption,
        ),
        patch(
            "custom_components.battery_controller.coordinator_forecast.dt_util.utcnow",
            return_value=now,
        ),
        patch(
            "custom_components.battery_controller.coordinator_forecast.dt_util.now",
            return_value=now,
        ),
        patch(
            "custom_components.battery_controller.coordinator_forecast.ir.async_delete_issue"
        ) as mock_delete_issue,
    ):
        coord = ForecastCoordinator(hass, weather_coord, _minimal_config())
        coord.net_load_model = MagicMock()
        coord.net_load_model.forecast = MagicMock(return_value=(None, [0.5] * 4, None))

        await coord._async_update_data()

    mock_delete_issue.assert_any_call(
        hass,
        "battery_controller",
        "weather_data_unavailable",
    )
    # Fresh (non-stale) weather data also clears the stale issue
    mock_delete_issue.assert_any_call(
        hass,
        "battery_controller",
        "weather_data_stale",
    )


@pytest.mark.asyncio
async def test_async_setup_subscribes_weather_coordinator(hass):
    """async_setup must register a listener on the weather coordinator.

    A DataUpdateCoordinator only keeps polling while it has listeners; without
    this subscription the weather coordinator fetches once at startup and then
    serves the same aging snapshot forever (while last_update_success stays
    True, so diagnostics report 'OK' on half-a-day-old data).
    """
    config = _minimal_config()
    weather_coord = MagicMock()
    weather_unsub = MagicMock()
    weather_coord.async_add_listener = MagicMock(return_value=weather_unsub)
    mock_consumption = _make_mock_consumption()

    with (
        patch(
            "custom_components.battery_controller.coordinator_forecast.ConsumptionForecastModel",
            return_value=mock_consumption,
        ),
        patch(
            "custom_components.battery_controller.coordinator_forecast.async_track_time_interval",
            return_value=MagicMock(),
        ),
    ):
        coord = ForecastCoordinator(hass, weather_coord, config)
        await coord.async_setup()

    weather_coord.async_add_listener.assert_called_once()
    assert coord._unsub_weather is weather_unsub

    # The listener triggers a forecast refresh when new weather data arrives
    listener = weather_coord.async_add_listener.call_args[0][0]
    with patch.object(coord, "async_request_refresh", AsyncMock()) as mock_refresh:
        listener()
        await hass.async_block_till_done()
    mock_refresh.assert_awaited_once()

    # And shutdown unsubscribes again
    await coord.async_shutdown()
    weather_unsub.assert_called_once()
    assert coord._unsub_weather is None


@pytest.mark.asyncio
async def test_async_update_data_stale_weather_creates_issue(hass):
    """Weather data older than the stale limit should raise a repair issue."""
    now = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
    weather_coord = MagicMock()
    weather_coord.data = {
        "radiation_forecast": [100.0] * 24,
        "dni_forecast": [50.0] * 24,
        "diffuse_forecast": [20.0] * 24,
        "wind_speed_forecast": [3.0] * 24,
        "temperature_forecast": [20.0] * 24,
        "forecast_start_utc": now - timedelta(hours=12),
        "timestamp": now - timedelta(hours=12),  # half a day old
    }
    mock_consumption = _make_mock_consumption()

    with (
        patch(
            "custom_components.battery_controller.coordinator_forecast.ConsumptionForecastModel",
            return_value=mock_consumption,
        ),
        patch(
            "custom_components.battery_controller.coordinator_forecast.dt_util.utcnow",
            return_value=now,
        ),
        patch(
            "custom_components.battery_controller.coordinator_forecast.dt_util.now",
            return_value=now,
        ),
        patch(
            "custom_components.battery_controller.coordinator_forecast.ir.async_create_issue"
        ) as mock_create_issue,
    ):
        coord = ForecastCoordinator(hass, weather_coord, _minimal_config())
        coord.net_load_model = MagicMock()
        coord.net_load_model.forecast = MagicMock(return_value=(None, [0.5] * 12, None))

        result = await coord._async_update_data()

    assert result is not None
    mock_create_issue.assert_called_once()
    assert mock_create_issue.call_args[0][2] == "weather_data_stale"
    placeholders = mock_create_issue.call_args[1]["translation_placeholders"]
    assert placeholders == {"age_hours": "12"}


@pytest.mark.asyncio
async def test_async_update_data_fresh_weather_no_stale_issue(hass):
    """Fresh weather data must not raise the stale-weather issue."""
    now = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
    weather_coord = MagicMock()
    weather_coord.data = {
        "radiation_forecast": [100.0] * 24,
        "dni_forecast": [50.0] * 24,
        "diffuse_forecast": [20.0] * 24,
        "wind_speed_forecast": [3.0] * 24,
        "temperature_forecast": [20.0] * 24,
        "forecast_start_utc": now,
        "timestamp": now - timedelta(minutes=20),
    }
    mock_consumption = _make_mock_consumption()

    with (
        patch(
            "custom_components.battery_controller.coordinator_forecast.ConsumptionForecastModel",
            return_value=mock_consumption,
        ),
        patch(
            "custom_components.battery_controller.coordinator_forecast.dt_util.utcnow",
            return_value=now,
        ),
        patch(
            "custom_components.battery_controller.coordinator_forecast.dt_util.now",
            return_value=now,
        ),
        patch(
            "custom_components.battery_controller.coordinator_forecast.ir.async_create_issue"
        ) as mock_create_issue,
    ):
        coord = ForecastCoordinator(hass, weather_coord, _minimal_config())
        coord.net_load_model = MagicMock()
        coord.net_load_model.forecast = MagicMock(return_value=(None, [0.5] * 24, None))

        await coord._async_update_data()

    mock_create_issue.assert_not_called()


@pytest.mark.asyncio
async def test_async_update_data_clamps_negative_pv_outputs(hass):
    """Negative PV model output should be clamped to zero."""
    pv_arrays = [
        {
            "subentry_id": "pv_ac_1",
            "peak_power_kwp": 4.0,
            "orientation": 180.0,
            "tilt": 35.0,
            "efficiency_factor": 0.85,
            "dc_coupled": False,
        }
    ]
    now = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
    weather_coord = MagicMock()
    weather_coord.data = {
        "radiation_forecast": [100.0, 150.0],
        "dni_forecast": [50.0, 60.0],
        "diffuse_forecast": [20.0, 25.0],
        "wind_speed_forecast": [3.0, 3.0],
        "temperature_forecast": [20.0, 20.0],
        "forecast_start_utc": now,
    }
    mock_consumption = _make_mock_consumption()

    with (
        patch(
            "custom_components.battery_controller.coordinator_forecast.ConsumptionForecastModel",
            return_value=mock_consumption,
        ),
        patch(
            "custom_components.battery_controller.coordinator_forecast.dt_util.utcnow",
            return_value=now,
        ),
        patch(
            "custom_components.battery_controller.coordinator_forecast.dt_util.now",
            return_value=now,
        ),
    ):
        coord = ForecastCoordinator(hass, weather_coord, _minimal_config(pv_arrays))
        coord.net_load_model = MagicMock()
        coord.net_load_model.forecast = MagicMock(return_value=(None, [0.5, 0.5], None))
        # 2 weather hours × 4 steps/hour = 8 forecast steps
        coord.pv_ac_models[0].forecast_from_radiation = MagicMock(
            return_value=[-1.0] + [0.2] * 7
        )

        result = await coord._async_update_data()

    assert result["pv_forecast_kw"] == [0.0] + [0.2] * 7
    assert result["per_pv_array_forecasts"]["pv_ac_1"] == [0.0] + [0.2] * 7


@pytest.mark.asyncio
async def test_async_update_data_without_subentry_id_omits_per_array_key(hass):
    """PV arrays without subentry_id still contribute to totals but not per-array map."""
    pv_arrays = [
        {
            "peak_power_kwp": 4.0,
            "orientation": 180.0,
            "tilt": 35.0,
            "efficiency_factor": 0.85,
            "dc_coupled": False,
        }
    ]
    now = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
    weather_coord = MagicMock()
    weather_coord.data = {
        "radiation_forecast": [100.0, 150.0],
        "dni_forecast": [50.0, 60.0],
        "diffuse_forecast": [20.0, 25.0],
        "wind_speed_forecast": [3.0, 3.0],
        "temperature_forecast": [20.0, 20.0],
        "forecast_start_utc": now,
    }
    mock_consumption = _make_mock_consumption()

    with (
        patch(
            "custom_components.battery_controller.coordinator_forecast.ConsumptionForecastModel",
            return_value=mock_consumption,
        ),
        patch(
            "custom_components.battery_controller.coordinator_forecast.dt_util.utcnow",
            return_value=now,
        ),
        patch(
            "custom_components.battery_controller.coordinator_forecast.dt_util.now",
            return_value=now,
        ),
    ):
        coord = ForecastCoordinator(hass, weather_coord, _minimal_config(pv_arrays))
        coord.net_load_model = MagicMock()
        coord.net_load_model.forecast = MagicMock(return_value=(None, [0.5, 0.5], None))
        # 2 weather hours × 4 steps/hour = 8 forecast steps
        coord.pv_ac_models[0].forecast_from_radiation = MagicMock(
            return_value=[0.1] * 4 + [0.2] * 4
        )

        result = await coord._async_update_data()

    assert result["pv_forecast_kw"] == [0.1] * 4 + [0.2] * 4
    assert result["per_pv_array_forecasts"] == {}


@pytest.mark.asyncio
async def test_async_update_data_quarter_hour_alignment(hass):
    """Forecast starts at the current quarter and expands hourly data per step."""
    config = _minimal_config()

    # 10:37 -> forecast must start at 10:30 (2 steps into the hour)
    now = datetime(2024, 6, 15, 10, 37, 0, tzinfo=timezone.utc)
    weather_data = {
        "radiation_forecast": [100.0, 200.0],
        "dni_forecast": [],
        "diffuse_forecast": [],
        "wind_speed_forecast": [3.0, 5.0],
        "temperature_forecast": [],
        "forecast_start_utc": datetime(2024, 6, 15, 10, 0, 0, tzinfo=timezone.utc),
    }
    weather_coord = MagicMock()
    weather_coord.data = weather_data

    with (
        patch(
            "custom_components.battery_controller.coordinator_forecast.ConsumptionForecastModel",
            return_value=_make_mock_consumption(),
        ),
        patch(
            "custom_components.battery_controller.coordinator_forecast.dt_util.utcnow",
            return_value=now,
        ),
        patch(
            "custom_components.battery_controller.coordinator_forecast.dt_util.now",
            return_value=now,
        ),
    ):
        coord = ForecastCoordinator(hass, weather_coord, config)
        coord.net_load_model = MagicMock()
        coord.net_load_model.forecast = MagicMock(return_value=(None, [0.4, 0.8], None))

        result = await coord._async_update_data()

    # 2 weather hours x 4 steps - 2 elapsed steps = 6 steps: 10:30..11:45
    assert result["forecast_interval_minutes"] == 15
    assert result["forecast_start_utc"] == datetime(
        2024, 6, 15, 10, 30, 0, tzinfo=timezone.utc
    )
    assert len(result["consumption_forecast_kw"]) == 6
    # First 2 steps belong to hour 10 (0.4), remaining 4 to hour 11 (0.8)
    assert result["consumption_forecast_kw"] == [0.4, 0.4, 0.8, 0.8, 0.8, 0.8]
    # current GHI reflects the current hour's radiation value
    assert result["current_ghi_wm2"] == 100.0


@pytest.mark.asyncio
async def test_forecast_coordinator_init_stores_forecast_sensors(hass):
    """PV forecast sensors from subentry data are stored per array."""
    pv_arrays = [
        {
            "subentry_id": "pv_ac_1",
            "peak_power_kwp": 4.0,
            "dc_coupled": False,
            "pv_forecast_sensors": [
                "sensor.solcast_pv_forecast_forecast_today",
                "sensor.solcast_pv_forecast_forecast_tomorrow",
            ],
        },
        {
            "subentry_id": "pv_dc_1",
            "peak_power_kwp": 3.0,
            "dc_coupled": True,
            "pv_forecast_sensors": ["sensor.solcast_dc"],
        },
    ]
    config = _minimal_config(pv_arrays=pv_arrays)
    weather_coord = MagicMock()

    with patch(
        "custom_components.battery_controller.coordinator_forecast.ConsumptionForecastModel",
        return_value=_make_mock_consumption(),
    ):
        coord = ForecastCoordinator(hass, weather_coord, config)

    assert coord.pv_ac_forecast_sensors == [
        [
            "sensor.solcast_pv_forecast_forecast_today",
            "sensor.solcast_pv_forecast_forecast_tomorrow",
        ]
    ]
    assert coord.pv_dc_forecast_sensors == [["sensor.solcast_dc"]]


@pytest.mark.asyncio
async def test_async_update_data_uses_solcast_sensor_forecast(hass):
    """Sensor forecast data overrides the internal model at native resolution."""
    pv_arrays = [
        {
            "subentry_id": "pv_ac_1",
            "peak_power_kwp": 4.0,
            "orientation": 180.0,
            "tilt": 35.0,
            "efficiency_factor": 0.85,
            "dc_coupled": False,
            "pv_forecast_sensors": ["sensor.solcast_today"],
        }
    ]
    config = _minimal_config(pv_arrays=pv_arrays)

    now = datetime(2024, 6, 15, 10, 0, 0, tzinfo=timezone.utc)
    weather_data = {
        "radiation_forecast": [500.0] * 24,
        "dni_forecast": [],
        "diffuse_forecast": [],
        "wind_speed_forecast": [3.0] * 24,
        "temperature_forecast": [],
        "forecast_start_utc": now,
    }
    weather_coord = MagicMock()
    weather_coord.data = weather_data

    # Solcast-style 30-min data covering the first two forecast hours
    hass.states.async_set(
        "sensor.solcast_today",
        "12.3",
        {
            "detailedForecast": [
                {"period_start": now.isoformat(), "pv_estimate": 2.0},
                {
                    "period_start": (now + timedelta(minutes=30)).isoformat(),
                    "pv_estimate": 3.0,
                },
                {
                    "period_start": (now + timedelta(hours=1)).isoformat(),
                    "pv_estimate": 1.0,
                },
            ]
        },
    )

    with (
        patch(
            "custom_components.battery_controller.coordinator_forecast.ConsumptionForecastModel",
            return_value=_make_mock_consumption(),
        ),
        patch(
            "custom_components.battery_controller.coordinator_forecast.dt_util.utcnow",
            return_value=now,
        ),
        patch(
            "custom_components.battery_controller.coordinator_forecast.dt_util.now",
            return_value=now,
        ),
    ):
        coord = ForecastCoordinator(hass, weather_coord, config)
        coord.net_load_model = MagicMock()
        coord.net_load_model.forecast = MagicMock(return_value=(None, [0.5] * 24, None))

        result = await coord._async_update_data()

    # 30-min Solcast periods map onto two 15-min steps each — no averaging
    assert result["pv_forecast_kw"][0] == pytest.approx(2.0)  # 10:00
    assert result["pv_forecast_kw"][1] == pytest.approx(2.0)  # 10:15
    assert result["pv_forecast_kw"][2] == pytest.approx(3.0)  # 10:30
    assert result["pv_forecast_kw"][3] == pytest.approx(3.0)  # 10:45
    # Last entry (11:00) covers one 30-min period (same as previous spacing)
    assert result["pv_forecast_kw"][4] == pytest.approx(1.0)  # 11:00
    assert result["pv_forecast_kw"][5] == pytest.approx(1.0)  # 11:15
    # 11:30+: past the sensor horizon -> internal model (500 W/m2 -> > 0 kW)
    assert result["pv_forecast_kw"][6] > 0.0
    assert result["per_pv_array_forecasts"]["pv_ac_1"][0] == pytest.approx(2.0)


@pytest.mark.asyncio
async def test_async_update_data_sensor_forecast_fallback_when_no_data(hass):
    """Missing/empty forecast sensors fall back to the internal model."""
    pv_arrays = [
        {
            "subentry_id": "pv_ac_1",
            "peak_power_kwp": 4.0,
            "orientation": 180.0,
            "tilt": 35.0,
            "efficiency_factor": 0.85,
            "dc_coupled": False,
            "pv_forecast_sensors": ["sensor.solcast_missing"],
        }
    ]
    config = _minimal_config(pv_arrays=pv_arrays)

    now = datetime(2024, 6, 15, 10, 0, 0, tzinfo=timezone.utc)
    weather_data = {
        "radiation_forecast": [500.0] * 24,
        "dni_forecast": [],
        "diffuse_forecast": [],
        "wind_speed_forecast": [3.0] * 24,
        "temperature_forecast": [],
        "forecast_start_utc": now,
    }
    weather_coord = MagicMock()
    weather_coord.data = weather_data

    with (
        patch(
            "custom_components.battery_controller.coordinator_forecast.ConsumptionForecastModel",
            return_value=_make_mock_consumption(),
        ),
        patch(
            "custom_components.battery_controller.coordinator_forecast.dt_util.utcnow",
            return_value=now,
        ),
        patch(
            "custom_components.battery_controller.coordinator_forecast.dt_util.now",
            return_value=now,
        ),
    ):
        coord = ForecastCoordinator(hass, weather_coord, config)
        coord.net_load_model = MagicMock()
        coord.net_load_model.forecast = MagicMock(return_value=(None, [0.5] * 24, None))

        result = await coord._async_update_data()

    # Internal model output at 500 W/m2 midday must be non-zero
    assert result["pv_forecast_kw"][0] > 0.0


@pytest.mark.asyncio
async def test_async_update_data_solcast_works_without_weather(hass):
    """Sensor forecast still applies when open-meteo weather data is missing."""
    pv_arrays = [
        {
            "subentry_id": "pv_ac_1",
            "peak_power_kwp": 4.0,
            "dc_coupled": False,
            "pv_forecast_sensors": ["sensor.solcast_today"],
        }
    ]
    config = _minimal_config(pv_arrays=pv_arrays)

    now = datetime(2024, 6, 15, 10, 0, 0, tzinfo=timezone.utc)
    weather_coord = MagicMock()
    weather_coord.data = None

    hass.states.async_set(
        "sensor.solcast_today",
        "12.3",
        {
            "detailedForecast": [
                {"period_start": now.isoformat(), "pv_estimate": 2.0},
            ]
        },
    )

    with (
        patch(
            "custom_components.battery_controller.coordinator_forecast.ConsumptionForecastModel",
            return_value=_make_mock_consumption(),
        ),
        patch(
            "custom_components.battery_controller.coordinator_forecast.dt_util.utcnow",
            return_value=now,
        ),
        patch(
            "custom_components.battery_controller.coordinator_forecast.dt_util.now",
            return_value=now,
        ),
    ):
        coord = ForecastCoordinator(hass, weather_coord, config)
        coord.net_load_model = MagicMock()
        coord.net_load_model.forecast = MagicMock(return_value=(None, [0.5] * 48, None))

        result = await coord._async_update_data()

    # Weather fallback is zero radiation, but the single Solcast entry covers
    # (at most) one hour: steps 10:00-10:45 get 2.0 kW, 11:00 falls back to 0
    assert result["pv_forecast_kw"][0] == pytest.approx(2.0)
    assert result["pv_forecast_kw"][3] == pytest.approx(2.0)
    assert result["pv_forecast_kw"][4] == 0.0


@pytest.mark.asyncio
async def test_async_update_data_volcast_5min_data_averaged_per_step(hass):
    """Sub-step (5-min Volcast) entries are averaged within each 15-min step."""
    pv_arrays = [
        {
            "subentry_id": "pv_ac_1",
            "peak_power_kwp": 4.0,
            "dc_coupled": False,
            "pv_forecast_sensors": ["sensor.volcast_energy_today"],
        }
    ]
    config = _minimal_config(pv_arrays=pv_arrays)

    now = datetime(2024, 6, 15, 10, 0, 0, tzinfo=timezone.utc)
    weather_coord = MagicMock()
    weather_coord.data = None

    # Volcast-style 5-min detailedForecast entries with power_w values:
    # 10:00-10:10 -> 1000/2000/3000 W (mean 2.0 kW for step 0)
    # 10:15-10:25 -> 4000 W each (mean 4.0 kW for step 1)
    hass.states.async_set(
        "sensor.volcast_energy_today",
        "12.3",
        {
            "detailedForecast": [
                {
                    "period_start": (now + timedelta(minutes=5 * i)).isoformat(),
                    "power_w": w,
                }
                for i, w in enumerate([1000, 2000, 3000, 4000, 4000, 4000])
            ]
        },
    )

    with (
        patch(
            "custom_components.battery_controller.coordinator_forecast.ConsumptionForecastModel",
            return_value=_make_mock_consumption(),
        ),
        patch(
            "custom_components.battery_controller.coordinator_forecast.dt_util.utcnow",
            return_value=now,
        ),
        patch(
            "custom_components.battery_controller.coordinator_forecast.dt_util.now",
            return_value=now,
        ),
    ):
        coord = ForecastCoordinator(hass, weather_coord, config)
        coord.net_load_model = MagicMock()
        coord.net_load_model.forecast = MagicMock(return_value=(None, [0.5] * 48, None))

        result = await coord._async_update_data()

    assert result["pv_forecast_kw"][0] == pytest.approx(2.0)
    assert result["pv_forecast_kw"][1] == pytest.approx(4.0)
    # Last 5-min entry (10:25) covers 5 minutes: step 10:30 is past the
    # sensor horizon and falls back to the (zero-radiation) internal model
    assert result["pv_forecast_kw"][2] == 0.0


@pytest.mark.asyncio
async def test_net_load_accounts_for_dc_coupled_pv(hass):
    """Net load must count DC PV, which reaches AC through the inverter.

    A DC-coupled system has pv_forecast_kw legitimately at zero with all
    production in pv_dc_forecast_kw. Subtracting only the AC series reported
    the household as importing its full load while the sun was covering it.
    """
    from custom_components.battery_controller.const import (
        DC_TO_AC_INVERTER_EFFICIENCY,
    )

    pv_arrays = [
        {
            "subentry_id": "pv_dc_1",
            "peak_power_kwp": 10.0,
            "dc_coupled": True,
        }
    ]
    config = _minimal_config(pv_arrays=pv_arrays)

    now = datetime(2026, 7, 27, 12, 0, 0, tzinfo=timezone.utc)
    weather_coord = MagicMock()
    weather_coord.data = None  # zero-radiation fallback; DC comes from the mock

    with (
        patch(
            "custom_components.battery_controller.coordinator_forecast.ConsumptionForecastModel",
            return_value=_make_mock_consumption(),
        ),
        patch(
            "custom_components.battery_controller.coordinator_forecast.dt_util.utcnow",
            return_value=now,
        ),
        patch(
            "custom_components.battery_controller.coordinator_forecast.dt_util.now",
            return_value=now,
        ),
    ):
        coord = ForecastCoordinator(hass, weather_coord, config)
        coord.net_load_model = MagicMock()
        coord.net_load_model.forecast = MagicMock(
            return_value=(None, [4.0] * 192, None)
        )
        # 10 kW of DC production, no AC array at all
        coord.pv_dc_models[0].forecast_from_radiation = MagicMock(
            return_value=[10.0] * 192
        )

        result = await coord._async_update_data()

    # The series uses the forecast (4.0 kW); the current_* values use
    # get_current_consumption() (0.5 kW from the mock) — existing behaviour.
    expected_series = 4.0 - 10.0 * DC_TO_AC_INVERTER_EFFICIENCY
    expected_current = 0.5 - 10.0 * DC_TO_AC_INVERTER_EFFICIENCY
    assert result["pv_forecast_kw"][0] == 0.0
    assert result["pv_dc_forecast_kw"][0] == pytest.approx(10.0)
    # A surplus, not a full-load import
    assert result["net_load_forecast_kw"][0] == pytest.approx(expected_series, abs=1e-3)
    assert result["current_net_load_kw"] == pytest.approx(expected_current, abs=1e-3)
    assert result["current_net_load_kw"] < 0


@pytest.mark.asyncio
async def test_net_load_unchanged_for_ac_only_system(hass):
    """AC-only systems keep the previous behaviour exactly."""
    pv_arrays = [
        {
            "subentry_id": "pv_ac_1",
            "peak_power_kwp": 4.0,
            "dc_coupled": False,
        }
    ]
    config = _minimal_config(pv_arrays=pv_arrays)

    now = datetime(2026, 7, 27, 12, 0, 0, tzinfo=timezone.utc)
    weather_coord = MagicMock()
    weather_coord.data = None

    with (
        patch(
            "custom_components.battery_controller.coordinator_forecast.ConsumptionForecastModel",
            return_value=_make_mock_consumption(),
        ),
        patch(
            "custom_components.battery_controller.coordinator_forecast.dt_util.utcnow",
            return_value=now,
        ),
        patch(
            "custom_components.battery_controller.coordinator_forecast.dt_util.now",
            return_value=now,
        ),
    ):
        coord = ForecastCoordinator(hass, weather_coord, config)
        coord.net_load_model = MagicMock()
        coord.net_load_model.forecast = MagicMock(
            return_value=(None, [2.0] * 192, None)
        )
        coord.pv_ac_models[0].forecast_from_radiation = MagicMock(
            return_value=[3.0] * 192
        )

        result = await coord._async_update_data()

    assert result["net_load_forecast_kw"][0] == pytest.approx(2.0 - 3.0)


class TestBatteryEnergySensors:
    """Collecting the per-subentry battery energy counters."""

    def test_empty_without_subentries(self):
        assert (
            _battery_energy_sensors(
                _minimal_config(), CONF_BATTERY_ENERGY_CHARGED_SENSOR
            )
            == []
        )

    def test_collects_one_per_subentry(self):
        config = _minimal_config(
            battery_subentries=[
                ("a", {CONF_BATTERY_ENERGY_CHARGED_SENSOR: "sensor.pack_a_in"}),
                ("b", {CONF_BATTERY_ENERGY_CHARGED_SENSOR: "sensor.pack_b_in"}),
            ]
        )
        assert _battery_energy_sensors(config, CONF_BATTERY_ENERGY_CHARGED_SENSOR) == [
            "sensor.pack_a_in",
            "sensor.pack_b_in",
        ]

    def test_skips_subentries_without_the_sensor(self):
        config = _minimal_config(
            battery_subentries=[
                ("a", {CONF_BATTERY_ENERGY_CHARGED_SENSOR: "sensor.pack_a_in"}),
                ("b", {}),
            ]
        )
        assert _battery_energy_sensors(config, CONF_BATTERY_ENERGY_CHARGED_SENSOR) == [
            "sensor.pack_a_in"
        ]

    def test_deduplicates_a_shared_inverter_counter(self):
        """One inverter counter selected on several packs must count once."""
        config = _minimal_config(
            battery_subentries=[
                ("a", {CONF_BATTERY_ENERGY_CHARGED_SENSOR: "sensor.inverter_in"}),
                ("b", {CONF_BATTERY_ENERGY_CHARGED_SENSOR: "sensor.inverter_in"}),
            ]
        )
        assert _battery_energy_sensors(config, CONF_BATTERY_ENERGY_CHARGED_SENSOR) == [
            "sensor.inverter_in"
        ]


# ---------------------------------------------------------------------------
# Per-array PV forecast calibration
# ---------------------------------------------------------------------------


def _pv_array(sid="pv1", kwp=4.0, measured="sensor.pv1_energy", dc=False):
    return {
        "subentry_id": sid,
        "peak_power_kwp": kwp,
        "orientation": 180.0,
        "tilt": 35.0,
        "efficiency_factor": 0.85,
        "dc_coupled": dc,
        CONF_PV_MEASURED_PRODUCTION_SENSOR: measured,
    }


def _make_cal_coordinator(hass, pv_arrays=None, legacy_pv_sensors=None):
    weather_coord = MagicMock()
    weather_coord.data = None
    config = _minimal_config(pv_arrays=pv_arrays or [_pv_array()])
    config["pv_production_sensors"] = legacy_pv_sensors or []
    with patch(
        "custom_components.battery_controller.coordinator_forecast.ConsumptionForecastModel",
        return_value=_make_mock_consumption(),
    ):
        return ForecastCoordinator(hass, weather_coord, config)


@pytest.mark.asyncio
async def test_measured_sensor_joins_the_reconstruction_without_migration(hass):
    """The per-array sensor is additive: the legacy list keeps working as-is."""
    weather_coord = MagicMock()
    weather_coord.data = None
    config = _minimal_config(pv_arrays=[_pv_array(measured="sensor.pv1_energy")])
    config["pv_production_sensors"] = ["sensor.legacy_total"]

    captured = {}

    def _capture(**kwargs):
        captured.update(kwargs)
        return _make_mock_consumption()

    with patch(
        "custom_components.battery_controller.coordinator_forecast.ConsumptionForecastModel",
        side_effect=_capture,
    ):
        ForecastCoordinator(hass, weather_coord, config)

    assert captured["pv_production_sensors"] == [
        "sensor.legacy_total",
        "sensor.pv1_energy",
    ]


@pytest.mark.asyncio
async def test_measured_sensor_is_deduplicated_against_the_legacy_list(hass):
    """The same entity configured in both places must be counted once."""
    weather_coord = MagicMock()
    weather_coord.data = None
    config = _minimal_config(pv_arrays=[_pv_array(measured="sensor.shared")])
    config["pv_production_sensors"] = ["sensor.shared"]

    captured = {}

    def _capture(**kwargs):
        captured.update(kwargs)
        return _make_mock_consumption()

    with patch(
        "custom_components.battery_controller.coordinator_forecast.ConsumptionForecastModel",
        side_effect=_capture,
    ):
        ForecastCoordinator(hass, weather_coord, config)

    assert captured["pv_production_sensors"] == ["sensor.shared"]


@pytest.mark.asyncio
async def test_pv_calibration_learns_an_energy_weighted_gain(hass):
    """A systematically over-forecast array converges on the measured ratio."""
    coord = _make_cal_coordinator(hass)
    now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    step_h = 0.25
    meter = 100.0

    # 30 quarter-hours where the array delivers 80 % of the forecast.
    for i in range(30):
        forecast_kw = 2.0  # 50 % of the 4 kWp rating: inside the sampling band
        coord._pv_cal_snapshot = {
            "taken_at": now,
            "forecast_kwh": {"pv1": forecast_kw * step_h},
            "meter_kwh": {"pv1": meter},
        }
        meter += 0.8 * forecast_kw * step_h
        hass.states.async_set(
            "sensor.pv1_energy", f"{meter}", {"unit_of_measurement": "kWh"}
        )
        now += timedelta(minutes=15)
        coord._update_pv_calibration(now)

    assert coord._pv_cal_correction["pv1"] == pytest.approx(0.8, abs=1e-6)


@pytest.mark.asyncio
async def test_pv_calibration_waits_for_enough_samples(hass):
    """Below the sample floor no correction is applied at all."""
    coord = _make_cal_coordinator(hass)
    now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    meter = 100.0
    for _ in range(5):
        coord._pv_cal_snapshot = {
            "taken_at": now,
            "forecast_kwh": {"pv1": 0.5},
            "meter_kwh": {"pv1": meter},
        }
        meter += 0.4
        hass.states.async_set(
            "sensor.pv1_energy", f"{meter}", {"unit_of_measurement": "kWh"}
        )
        now += timedelta(minutes=15)
        coord._update_pv_calibration(now)

    assert len(coord._pv_cal_samples["pv1"]) == 5
    assert "pv1" not in coord._pv_cal_correction


@pytest.mark.asyncio
async def test_pv_calibration_skipped_while_curtailed(hass):
    """Deliberately suppressed production is not a forecast error."""
    coord = _make_cal_coordinator(hass)
    coord.pv_curtailed = True
    now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    coord._pv_cal_snapshot = {
        "taken_at": now,
        "forecast_kwh": {"pv1": 0.5},
        "meter_kwh": {"pv1": 100.0},
    }
    hass.states.async_set("sensor.pv1_energy", "100.0", {"unit_of_measurement": "kWh"})
    coord._update_pv_calibration(now + timedelta(minutes=15))

    assert "pv1" not in coord._pv_cal_samples


@pytest.mark.asyncio
async def test_pv_calibration_ignores_a_mismatched_window(hass):
    """If the elapsed time is not the window the forecast described, skip."""
    coord = _make_cal_coordinator(hass)
    now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    coord._pv_cal_snapshot = {
        "taken_at": now,
        "forecast_kwh": {"pv1": 0.5},
        "meter_kwh": {"pv1": 100.0},
    }
    hass.states.async_set("sensor.pv1_energy", "100.4", {"unit_of_measurement": "kWh"})
    coord._update_pv_calibration(now + timedelta(hours=2))

    assert "pv1" not in coord._pv_cal_samples


@pytest.mark.asyncio
async def test_pv_calibration_ignores_a_meter_reset(hass):
    coord = _make_cal_coordinator(hass)
    now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    coord._pv_cal_snapshot = {
        "taken_at": now,
        "forecast_kwh": {"pv1": 0.5},
        "meter_kwh": {"pv1": 100.0},
    }
    hass.states.async_set("sensor.pv1_energy", "0.2", {"unit_of_measurement": "kWh"})
    coord._update_pv_calibration(now + timedelta(minutes=15))

    assert "pv1" not in coord._pv_cal_samples


@pytest.mark.asyncio
async def test_pv_calibration_drops_an_absurd_sample(hass):
    """Five times the forecast in one step is not a gain error."""
    coord = _make_cal_coordinator(hass)
    now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    coord._pv_cal_snapshot = {
        "taken_at": now,
        "forecast_kwh": {"pv1": 0.5},
        "meter_kwh": {"pv1": 100.0},
    }
    hass.states.async_set("sensor.pv1_energy", "110.0", {"unit_of_measurement": "kWh"})
    coord._update_pv_calibration(now + timedelta(minutes=15))

    assert "pv1" not in coord._pv_cal_samples


@pytest.mark.asyncio
async def test_pv_snapshot_only_inside_the_sampling_band(hass):
    """Night-time and near-rating steps are not recorded."""
    coord = _make_cal_coordinator(hass)
    now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    hass.states.async_set("sensor.pv1_energy", "100.0", {"unit_of_measurement": "kWh"})

    # 0.1 kW on a 4 kWp array = 2.5 %: below the floor.
    coord._snapshot_pv_calibration(now, {"pv1": 0.1}, {"pv1": 4.0})
    assert coord._pv_cal_snapshot == {}

    # 3.8 kW = 95 %: clipping and thermal derating territory.
    coord._snapshot_pv_calibration(now, {"pv1": 3.8}, {"pv1": 4.0})
    assert coord._pv_cal_snapshot == {}

    # 2.0 kW = 50 %: recorded.
    coord._snapshot_pv_calibration(now, {"pv1": 2.0}, {"pv1": 4.0})
    assert coord._pv_cal_snapshot["forecast_kwh"]["pv1"] == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_pv_correction_is_clamped(hass):
    """However bad the ratio, the applied factor stays inside its bounds."""
    coord = _make_cal_coordinator(hass)
    now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    meter = 100.0
    for _ in range(25):
        coord._pv_cal_snapshot = {
            "taken_at": now,
            "forecast_kwh": {"pv1": 1.0},
            "meter_kwh": {"pv1": meter},
        }
        meter += 0.1  # only a tenth of the forecast, every time
        hass.states.async_set(
            "sensor.pv1_energy", f"{meter}", {"unit_of_measurement": "kWh"}
        )
        now += timedelta(minutes=15)
        coord._update_pv_calibration(now)

    assert coord._pv_cal_correction["pv1"] == pytest.approx(0.5)


@pytest.mark.asyncio
async def test_pv_calibration_reset(hass):
    coord = _make_cal_coordinator(hass)
    coord._pv_cal_correction["pv1"] = 0.8
    coord._pv_cal_samples["pv1"] = deque([(1.0, 1.25)], maxlen=10)
    coord._pv_cal_store.async_save = AsyncMock()

    await coord.async_reset_pv_calibration()

    assert coord._pv_cal_correction == {}
    assert coord._pv_cal_samples == {}

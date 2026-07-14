"""Tests for ForecastCoordinator."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


from custom_components.battery_controller.coordinator_forecast import (
    ForecastCoordinator,
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
    await coord.async_shutdown()  # Should not raise


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

    # Should succeed with zero PV forecast
    assert result is not None
    assert result["pv_forecast_kw"] == [0.0] * 48
    assert result["consumption_forecast_kw"] == [0.5] * 48


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
    """When weather data is older than 0 hours, forecasts are shifted (lines 152-163)."""
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

    # Forecast was shifted by 2 hours, so 48-2=46 hours remain
    assert "pv_forecast_kw" in result
    assert len(result["pv_forecast_kw"]) == 46


@pytest.mark.asyncio
async def test_async_update_data_with_ac_pv_model(hass):
    """ForecastCoordinator processes AC PV arrays in _async_update_data (lines 189-202)."""
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
    """ForecastCoordinator processes DC PV arrays in _async_update_data (lines 213-226)."""
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

    assert result["pv_forecast_kw"] == [0.0] * 48
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
        coord.pv_ac_models[0].forecast_from_radiation = MagicMock(
            return_value=[-1.0, 0.2]
        )

        result = await coord._async_update_data()

    assert result["pv_forecast_kw"] == [0.0, 0.2]
    assert result["per_pv_array_forecasts"]["pv_ac_1"] == [0.0, 0.2]


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
        coord.pv_ac_models[0].forecast_from_radiation = MagicMock(
            return_value=[0.1, 0.2]
        )

        result = await coord._async_update_data()

    assert result["pv_forecast_kw"] == [0.1, 0.2]
    assert result["per_pv_array_forecasts"] == {}

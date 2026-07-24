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

    # Forecast was shifted by 2 hours, so 48-2=46 hours remain (at 15-min steps)
    assert "pv_forecast_kw" in result
    assert len(result["pv_forecast_kw"]) == 46 * 4


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

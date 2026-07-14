"""Tests for WeatherDataCoordinator."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.battery_controller.coordinator_weather import (
    WeatherDataCoordinator,
)


def _make_weather_response(
    times=None,
    radiation=None,
    dni=None,
    diffuse=None,
    wind=None,
    temperature=None,
):
    """Build a minimal open-meteo API response dict."""
    now_hour = datetime(2024, 6, 15, 0, 0, 0, tzinfo=timezone.utc)
    if times is None:
        from datetime import timedelta

        times = [
            (now_hour + timedelta(hours=i)).isoformat().replace("+00:00", "")
            for i in range(48)
        ]
    if radiation is None:
        radiation = [float(100 + i) for i in range(48)]
    hourly: dict = {
        "time": times,
        "shortwave_radiation": radiation,
    }
    if dni is not None:
        hourly["direct_normal_irradiance"] = dni
    if diffuse is not None:
        hourly["diffuse_radiation"] = diffuse
    if wind is not None:
        hourly["wind_speed_10m"] = wind
    if temperature is not None:
        hourly["temperature_2m"] = temperature
    return {"hourly": hourly}


@pytest.mark.asyncio
async def test_weather_coordinator_init(hass):
    """WeatherDataCoordinator initializes with hass config lat/lon."""
    with patch(
        "custom_components.battery_controller.coordinator_weather.async_get_clientsession",
        return_value=MagicMock(),
    ):
        coord = WeatherDataCoordinator(hass)

    assert coord.latitude == hass.config.latitude
    assert coord.longitude == hass.config.longitude


@pytest.mark.asyncio
async def test_async_update_data_success(hass):
    """_async_update_data returns forecast dict on successful API call."""
    response_data = _make_weather_response(
        dni=[float(50 + i) for i in range(48)],
        diffuse=[float(20 + i) for i in range(48)],
        wind=[float(3 + i * 0.1) for i in range(48)],
        temperature=[float(15 + i * 0.1) for i in range(48)],
    )

    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value=response_data)
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=None)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_resp)

    with (
        patch(
            "custom_components.battery_controller.coordinator_weather.async_get_clientsession",
            return_value=mock_session,
        ),
        patch(
            "custom_components.battery_controller.coordinator_weather.dt_util.utcnow",
            return_value=datetime(2024, 6, 15, 10, 30, 0, tzinfo=timezone.utc),
        ),
    ):
        coord = WeatherDataCoordinator(hass)
        result = await coord._async_update_data()

    assert "radiation_forecast" in result
    assert "wind_speed_forecast" in result
    assert "temperature_forecast" in result
    assert "forecast_start_utc" in result
    assert len(result["radiation_forecast"]) <= 48


@pytest.mark.asyncio
async def test_async_update_data_non_200_status(hass):
    """Non-200 status raises UpdateFailed."""
    mock_resp = AsyncMock()
    mock_resp.status = 503
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=None)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_resp)

    with patch(
        "custom_components.battery_controller.coordinator_weather.async_get_clientsession",
        return_value=mock_session,
    ):
        coord = WeatherDataCoordinator(hass)

    with pytest.raises(UpdateFailed):
        await coord._async_update_data()


@pytest.mark.asyncio
async def test_async_update_data_client_error(hass):
    """aiohttp.ClientError raises UpdateFailed."""
    import aiohttp

    mock_session = MagicMock()
    mock_session.get = MagicMock(side_effect=aiohttp.ClientError("connection refused"))

    with patch(
        "custom_components.battery_controller.coordinator_weather.async_get_clientsession",
        return_value=mock_session,
    ):
        coord = WeatherDataCoordinator(hass)

    with pytest.raises(UpdateFailed):
        await coord._async_update_data()


@pytest.mark.asyncio
async def test_async_update_data_timeout_error(hass):
    """TimeoutError raises UpdateFailed."""
    mock_session = MagicMock()
    mock_session.get = MagicMock(side_effect=TimeoutError("request timed out"))

    with patch(
        "custom_components.battery_controller.coordinator_weather.async_get_clientsession",
        return_value=mock_session,
    ):
        coord = WeatherDataCoordinator(hass)

    with pytest.raises(UpdateFailed):
        await coord._async_update_data()


@pytest.mark.asyncio
async def test_async_update_data_no_radiation(hass):
    """Empty radiation data raises UpdateFailed."""
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(
        return_value={"hourly": {"time": [], "shortwave_radiation": []}}
    )
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=None)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_resp)

    with patch(
        "custom_components.battery_controller.coordinator_weather.async_get_clientsession",
        return_value=mock_session,
    ):
        coord = WeatherDataCoordinator(hass)

    with pytest.raises(UpdateFailed):
        await coord._async_update_data()


@pytest.mark.asyncio
async def test_async_shutdown_stops_polling(hass):
    """async_shutdown uses the base-class implementation (cancels the timer)."""
    with patch(
        "custom_components.battery_controller.coordinator_weather.async_get_clientsession",
        return_value=MagicMock(),
    ):
        coord = WeatherDataCoordinator(hass)

    await coord.async_shutdown()  # Should not raise
    assert coord._shutdown_requested is True


@pytest.mark.asyncio
async def test_async_update_data_without_optional_fields(hass):
    """Missing optional fields (dni/diffuse/wind/temp) default to zeros."""
    response_data = _make_weather_response()  # Only radiation, no optional fields

    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value=response_data)
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=None)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_resp)

    with (
        patch(
            "custom_components.battery_controller.coordinator_weather.async_get_clientsession",
            return_value=mock_session,
        ),
        patch(
            "custom_components.battery_controller.coordinator_weather.dt_util.utcnow",
            return_value=datetime(2024, 6, 15, 10, 0, 0, tzinfo=timezone.utc),
        ),
    ):
        coord = WeatherDataCoordinator(hass)
        result = await coord._async_update_data()

    # Optional fields default to zeros
    assert all(v == 0.0 for v in result["dni_forecast"])
    assert all(v == 0.0 for v in result["diffuse_forecast"])
    assert all(v == 0.0 for v in result["wind_speed_forecast"])
    assert result["temperature_forecast"] == []


@pytest.mark.asyncio
async def test_start_idx_logic(hass):
    """start_idx selects the correct current hour in the forecast."""
    base = datetime(2024, 6, 15, 0, 0, 0, tzinfo=timezone.utc)
    times = [
        (base.replace(hour=i)).isoformat().replace("+00:00", "") for i in range(24)
    ]
    # Radiation increases hourly so we can check which hour was selected
    radiation = [float(i * 10) for i in range(24)]

    response_data = {"hourly": {"time": times, "shortwave_radiation": radiation}}

    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value=response_data)
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=None)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_resp)

    # Now is 10:00 UTC → start_idx should be 10
    with (
        patch(
            "custom_components.battery_controller.coordinator_weather.async_get_clientsession",
            return_value=mock_session,
        ),
        patch(
            "custom_components.battery_controller.coordinator_weather.dt_util.utcnow",
            return_value=datetime(2024, 6, 15, 10, 30, 0, tzinfo=timezone.utc),
        ),
    ):
        coord = WeatherDataCoordinator(hass)
        result = await coord._async_update_data()

    # First value should be radiation at index 10 = 100.0
    assert result["radiation_forecast"][0] == pytest.approx(100.0)


@pytest.mark.asyncio
async def test_async_update_data_invalid_timestamp_skipped(hass):
    """Invalid timestamps in weather response are skipped (lines 81-82)."""
    from unittest.mock import AsyncMock, MagicMock

    response_data = {
        "hourly": {
            "time": [
                "not-a-valid-timestamp",  # triggers ValueError → continue (line 82)
                "2024-06-15T11:00:00",
                "2024-06-15T12:00:00",
            ],
            "shortwave_radiation": [0.0, 200.0, 300.0],
            "direct_normal_irradiance": [0.0, 150.0, 250.0],
            "diffuse_radiation": [0.0, 50.0, 60.0],
            "temperature_2m": [15.0, 20.0, 22.0],
            "windspeed_10m": [3.0, 4.0, 5.0],
        }
    }

    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value=response_data)
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=None)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_resp)

    # Set now to 2024-06-15T10:30:00 so index search finds the valid "11:00" entry
    with (
        patch(
            "custom_components.battery_controller.coordinator_weather.async_get_clientsession",
            return_value=mock_session,
        ),
        patch(
            "custom_components.battery_controller.coordinator_weather.dt_util.utcnow",
            return_value=datetime(2024, 6, 15, 10, 30, 0, tzinfo=timezone.utc),
        ),
    ):
        coord = WeatherDataCoordinator(hass)
        result = await coord._async_update_data()

    # Invalid timestamp is skipped; start_idx advances to the valid "11:00" entry
    assert result is not None
    assert len(result["radiation_forecast"]) > 0


@pytest.mark.asyncio
async def test_async_update_data_invalid_radiation_value_raises_update_failed(hass):
    """Non-numeric radiation values should be surfaced as UpdateFailed."""
    mock_resp = AsyncMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(
        return_value={
            "hourly": {
                "time": ["2024-06-15T10:00:00", "2024-06-15T11:00:00"],
                "shortwave_radiation": ["bad-value", 200.0],
            }
        }
    )
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=None)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_resp)

    with (
        patch(
            "custom_components.battery_controller.coordinator_weather.async_get_clientsession",
            return_value=mock_session,
        ),
        patch(
            "custom_components.battery_controller.coordinator_weather.dt_util.utcnow",
            return_value=datetime(2024, 6, 15, 10, 0, 0, tzinfo=timezone.utc),
        ),
    ):
        coord = WeatherDataCoordinator(hass)

    with pytest.raises(UpdateFailed):
        await coord._async_update_data()

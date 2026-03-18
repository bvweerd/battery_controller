"""Weather data coordinator for the Battery Controller integration."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)


class WeatherDataCoordinator(DataUpdateCoordinator):
    """Coordinator for weather and radiation data from open-meteo.com."""

    def __init__(self, hass: HomeAssistant):
        """Initialize the weather data coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name="Battery Controller Weather",
            update_interval=timedelta(minutes=30),
        )
        self.latitude = hass.config.latitude
        self.longitude = hass.config.longitude
        self.session = async_get_clientsession(hass)

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch weather and radiation data from open-meteo.com."""
        _LOGGER.debug(
            "Fetching weather data for %.4f, %.4f", self.latitude, self.longitude
        )

        url = "https://api.open-meteo.com/v1/forecast?" + urlencode(
            {
                "latitude": self.latitude,
                "longitude": self.longitude,
                "hourly": "temperature_2m,shortwave_radiation,wind_speed_10m",
                "wind_speed_unit": "ms",
                "current_weather": "true",
                "timezone": "UTC",
                "forecast_days": "2",
            }
        )

        try:
            async with self.session.get(
                url, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    raise UpdateFailed(f"API returned status {resp.status}")
                data = await resp.json()
        except (aiohttp.ClientError, TimeoutError) as err:
            raise UpdateFailed(f"Error fetching weather data: {err}")

        # Extract hourly forecasts
        hourly = data.get("hourly", {})
        times = hourly.get("time", [])
        radiation = hourly.get("shortwave_radiation", [])
        wind_speed = hourly.get("wind_speed_10m", [])

        if not times or not radiation:
            raise UpdateFailed("No forecast data in API response")

        # Find current hour index
        now = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
        start_idx = 0
        for i, ts in enumerate(times):
            try:
                t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if t.tzinfo is None:
                    t = t.replace(tzinfo=dt_util.UTC)
            except ValueError:
                continue
            if t >= now:
                start_idx = i
                break

        # Extract next 48 hours
        radiation_forecast = [float(v) for v in radiation[start_idx : start_idx + 48]]
        wind_speed_forecast = (
            [float(v) for v in wind_speed[start_idx : start_idx + 48]]
            if wind_speed
            else [0.0] * len(radiation_forecast)
        )
        temperature = hourly.get("temperature_2m", [])
        temperature_forecast = (
            [float(v) for v in temperature[start_idx : start_idx + 48]]
            if temperature
            else []
        )

        result = {
            "radiation_forecast": [round(v, 1) for v in radiation_forecast],
            "wind_speed_forecast": [round(v, 1) for v in wind_speed_forecast],
            "temperature_forecast": [round(v, 1) for v in temperature_forecast],
            "forecast_start_utc": now,
            "timestamp": dt_util.utcnow(),
        }

        _LOGGER.debug(
            "Weather data updated: %d hours of radiation/wind forecast",
            len(radiation_forecast),
        )

        return result

    async def async_shutdown(self) -> None:
        """Cancel the periodic update timer."""
        pass

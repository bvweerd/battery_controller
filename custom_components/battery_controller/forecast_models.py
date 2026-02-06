"""Forecast models for PV production and consumption."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .helpers import calculate_pv_forecast, calculate_consumption_pattern

_LOGGER = logging.getLogger(__name__)


class PVForecastModel:
    """Model for PV production forecasting."""

    def __init__(
        self,
        peak_power_kwp: float = 0.0,
        orientation_deg: float = 180,
        tilt_deg: float = 35,
        efficiency_factor: float = 0.85,
    ):
        """Initialize PV forecast model."""
        self.peak_power_kwp = peak_power_kwp
        self.orientation_deg = orientation_deg
        self.tilt_deg = tilt_deg
        self.efficiency_factor = efficiency_factor

    def forecast_from_radiation(
        self,
        radiation_forecast: list[float],
    ) -> list[float]:
        """Generate PV forecast from solar radiation data.

        Args:
            radiation_forecast: Solar radiation in W/m2

        Returns:
            PV production forecast in kW
        """
        return calculate_pv_forecast(
            radiation_forecast,
            self.peak_power_kwp,
            self.orientation_deg,
            self.tilt_deg,
            self.efficiency_factor,
        )


class ConsumptionForecastModel:
    """Model for household consumption forecasting.

    Uses DSMR-style energy sensors (kWh, total_increasing) for pattern learning.
    Net consumption = sum(consumption_sensors) - sum(production_sensors).
    Hourly kWh change from HA statistics equals average kW during that hour.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        consumption_sensors: list[str] | None = None,
        production_sensors: list[str] | None = None,
        history_days: int = 14,
        base_consumption_kw: float = 0.5,
    ):
        """Initialize consumption forecast model."""
        self.hass = hass
        self.consumption_sensors = consumption_sensors or []
        self.production_sensors = production_sensors or []
        self.history_days = history_days
        self.base_consumption_kw = base_consumption_kw
        self._hourly_pattern: dict[tuple[int, int], float] = {}

    async def async_update_pattern(self) -> None:
        """Update consumption pattern from historical energy data.

        Queries HA recorder statistics for hourly energy changes (kWh).
        Net consumption = sum(consumption) - sum(production) per hour.
        kWh per hour equals average kW, so values map directly to power.
        """
        all_sensors = self.consumption_sensors + self.production_sensors
        if not all_sensors:
            return

        try:
            from homeassistant.components.recorder import get_instance
            from homeassistant.components.recorder.statistics import (
                statistics_during_period,
            )

            end_time = dt_util.utcnow()
            start_time = end_time - timedelta(days=self.history_days)

            stats = await get_instance(self.hass).async_add_executor_job(
                statistics_during_period,
                self.hass,
                start_time,
                end_time,
                set(all_sensors),
                "hour",
                None,
                {"change"},
            )

            if not stats:
                _LOGGER.debug("No statistics found for energy sensors")
                return

            # Build per-hour net consumption: sum(consumption) - sum(production)
            # Each stat entry's "change" = kWh in that hour = avg kW
            hourly_net: dict[str, float] = {}  # key: ISO timestamp -> net kWh

            for sensor_id in self.consumption_sensors:
                for stat in stats.get(sensor_id, []):
                    change = stat.get("change")
                    if change is None:
                        continue
                    ts_key = str(stat.get("start", ""))
                    hourly_net[ts_key] = hourly_net.get(ts_key, 0.0) + float(change)

            for sensor_id in self.production_sensors:
                for stat in stats.get(sensor_id, []):
                    change = stat.get("change")
                    if change is None:
                        continue
                    ts_key = str(stat.get("start", ""))
                    hourly_net[ts_key] = hourly_net.get(ts_key, 0.0) - float(change)

            # Group by (hour, day_of_week)
            hourly_values: dict[tuple[int, int], list[float]] = {}

            for ts_key, net_kwh in hourly_net.items():
                dt = dt_util.parse_datetime(ts_key)
                if dt is None:
                    # Try parsing as datetime object (recorder may return datetime)
                    continue

                key = (dt.hour, dt.weekday())
                if key not in hourly_values:
                    hourly_values[key] = []
                # kWh per hour = average kW, clamp to >= 0
                hourly_values[key].append(max(0.0, net_kwh))

            # Also handle datetime objects from recorder
            for sensor_id in self.consumption_sensors:
                for stat in stats.get(sensor_id, []):
                    start = stat.get("start")
                    if isinstance(start, datetime) and str(start) not in hourly_net:
                        change = stat.get("change")
                        if change is None:
                            continue
                        key = (start.hour, start.weekday())
                        if key not in hourly_values:
                            hourly_values[key] = []
                        hourly_values[key].append(max(0.0, float(change)))

            # Calculate averages
            for key, values in hourly_values.items():
                if values:
                    self._hourly_pattern[key] = sum(values) / len(values)

            _LOGGER.debug(
                "Updated consumption pattern from %d energy sensors, %d data points",
                len(all_sensors),
                len(self._hourly_pattern),
            )

        except ImportError:
            _LOGGER.debug("Recorder not available for consumption pattern")
        except Exception as err:
            _LOGGER.warning("Failed to update consumption pattern: %s", err)

    def forecast(
        self,
        hours: int = 24,
        start_time: datetime | None = None,
    ) -> list[float]:
        """Generate consumption forecast.

        Args:
            hours: Number of hours to forecast
            start_time: Start time for forecast (default: now)

        Returns:
            Consumption forecast in kW
        """
        if start_time is None:
            start_time = dt_util.now()

        forecast = []
        for h in range(hours):
            dt = start_time + timedelta(hours=h)
            hour = dt.hour
            dow = dt.weekday()

            # Use historical pattern if available
            key = (hour, dow)
            if key in self._hourly_pattern:
                forecast.append(self._hourly_pattern[key])
            else:
                # Fall back to default pattern
                forecast.append(
                    calculate_consumption_pattern(hour, dow, self.base_consumption_kw)
                )

        return forecast

    def get_current_consumption(self) -> float:
        """Get current consumption estimate from learned pattern.

        DSMR energy sensors are cumulative kWh, so instantaneous power
        cannot be read directly. Uses the learned hourly pattern instead.

        Returns:
            Current consumption in kW
        """
        now = dt_util.now()
        key = (now.hour, now.weekday())
        if key in self._hourly_pattern:
            return self._hourly_pattern[key]
        return calculate_consumption_pattern(
            now.hour, now.weekday(), self.base_consumption_kw
        )


class NetLoadForecast:
    """Combined PV and consumption forecast for net load calculation."""

    def __init__(
        self,
        pv_model: PVForecastModel,
        consumption_model: ConsumptionForecastModel,
    ):
        """Initialize net load forecast."""
        self.pv_model = pv_model
        self.consumption_model = consumption_model

    def forecast(
        self,
        radiation_forecast: list[float],
        hours: int | None = None,
    ) -> tuple[list[float], list[float], list[float]]:
        """Generate net load forecast.

        Args:
            radiation_forecast: Solar radiation forecast in W/m2
            hours: Number of hours to forecast (default: len of radiation)

        Returns:
            Tuple of (pv_forecast, consumption_forecast, net_load_forecast)
            All in kW. net_load > 0 means import, < 0 means export.
        """
        if hours is None:
            hours = len(radiation_forecast)

        pv_forecast = self.pv_model.forecast_from_radiation(radiation_forecast[:hours])
        consumption_forecast = self.consumption_model.forecast(hours)

        # Pad forecasts if needed
        while len(pv_forecast) < hours:
            pv_forecast.append(0.0)
        while len(consumption_forecast) < hours:
            consumption_forecast.append(self.consumption_model.base_consumption_kw)

        # Net load = consumption - PV (positive = import needed)
        net_load_forecast = [c - p for c, p in zip(consumption_forecast, pv_forecast)]

        return pv_forecast, consumption_forecast, net_load_forecast

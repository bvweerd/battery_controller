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

    PV double-counting correction (three layers, first available wins):
    1. pv_production_sensors: real kWh sensors from inverter(s) → add back to net
    2. own pv_forecast sensor history (via entry_id lookup) → self-consistent correction
    3. Warning log when production_sensors configured without a correction method
    """

    def __init__(
        self,
        hass: HomeAssistant,
        consumption_sensors: list[str] | None = None,
        production_sensors: list[str] | None = None,
        history_days: int = 14,
        base_consumption_kw: float = 0.5,
        pv_production_sensors: list[str] | None = None,
        entry_id: str | None = None,
    ):
        """Initialize consumption forecast model."""
        self.hass = hass
        self.consumption_sensors = consumption_sensors or []
        self.production_sensors = production_sensors or []
        self.history_days = history_days
        self.base_consumption_kw = base_consumption_kw
        self.pv_production_sensors = pv_production_sensors or []
        self._entry_id = entry_id
        self._hourly_pattern: dict[tuple[int, int], float] = {}

    async def async_update_pattern(self) -> None:
        """Update consumption pattern from historical energy data.

        Queries HA recorder statistics for hourly energy changes (kWh).
        Net consumption = sum(consumption) - sum(production) per hour.
        kWh per hour equals average kW, so values map directly to power.

        If electricity_production_sensors are configured alongside a PV model,
        the learned value is net grid exchange (import - export = consumption - PV).
        To avoid double-counting when the optimizer subtracts PV forecast again,
        we add back the historical PV production (three-layer fallback):
          1. pv_production_sensors: real inverter kWh sensors (most accurate)
          2. own pv_forecast sensor history from HA recorder (self-consistent)
          3. Warning log if no correction is possible
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

            # Normalise a stat entry to (ts_key, value) regardless of whether
            # the recorder returns the "start" field as a string or datetime.
            def _ts_and_value(stat: dict, field: str) -> tuple[str, float] | None:
                value = stat.get(field)
                if value is None:
                    return None
                start = stat.get("start")
                if isinstance(start, datetime):
                    ts_key = start.isoformat()
                else:
                    ts_key = str(start or "")
                return ts_key, float(value)

            for sensor_id in self.consumption_sensors:
                for stat in stats.get(sensor_id, []):
                    result = _ts_and_value(stat, "change")
                    if result:
                        ts_key, val = result
                        hourly_net[ts_key] = hourly_net.get(ts_key, 0.0) + val

            for sensor_id in self.production_sensors:
                for stat in stats.get(sensor_id, []):
                    result = _ts_and_value(stat, "change")
                    if result:
                        ts_key, val = result
                        hourly_net[ts_key] = hourly_net.get(ts_key, 0.0) - val

            # PV correction: add back historical PV production so that the
            # stored pattern represents gross household consumption.
            # This prevents double-counting when the optimizer subtracts pv_forecast.
            pv_corrected = False
            if self.production_sensors and self.pv_production_sensors:
                # Layer 1: real PV inverter kWh sensors
                pv_stats = await get_instance(self.hass).async_add_executor_job(
                    statistics_during_period,
                    self.hass,
                    start_time,
                    end_time,
                    set(self.pv_production_sensors),
                    "hour",
                    None,
                    {"change"},
                )
                for sensor_id in self.pv_production_sensors:
                    for stat in pv_stats.get(sensor_id, []):
                        result = _ts_and_value(stat, "change")
                        if result:
                            ts_key, val = result
                            hourly_net[ts_key] = hourly_net.get(ts_key, 0.0) + max(
                                0.0, val
                            )
                pv_corrected = True
                _LOGGER.debug(
                    "PV correction applied from %d production sensor(s)",
                    len(self.pv_production_sensors),
                )
            elif self.production_sensors and self._entry_id:
                # Layer 2: own pv_forecast sensor history (state_class=MEASUREMENT)
                try:
                    from homeassistant.helpers import entity_registry as er

                    ent_reg = er.async_get(self.hass)
                    from .const import DOMAIN

                    pv_entity_id = ent_reg.async_get_entity_id(
                        "sensor", DOMAIN, f"{self._entry_id}_pv_forecast"
                    )
                    if pv_entity_id:
                        pv_stats = await get_instance(self.hass).async_add_executor_job(
                            statistics_during_period,
                            self.hass,
                            start_time,
                            end_time,
                            {pv_entity_id},
                            "hour",
                            None,
                            {"mean"},
                        )
                        for stat in pv_stats.get(pv_entity_id, []):
                            result = _ts_and_value(stat, "mean")
                            if result:
                                ts_key, val = result
                                # mean kW over 1 h = kWh for that hour
                                hourly_net[ts_key] = hourly_net.get(ts_key, 0.0) + max(
                                    0.0, val
                                )
                        pv_corrected = True
                        _LOGGER.debug(
                            "PV correction applied from own pv_forecast sensor (%s)",
                            pv_entity_id,
                        )
                except Exception as err:
                    _LOGGER.debug(
                        "Could not apply PV correction from forecast sensor: %s", err
                    )

            if self.production_sensors and not pv_corrected:
                # Layer 3: warn that double-counting may occur
                _LOGGER.warning(
                    "electricity_production_sensors are configured alongside a PV model "
                    "but no PV correction could be applied. This may cause double-counting "
                    "of PV in the consumption forecast. Configure 'pv_production_sensors' "
                    "with your inverter's total energy sensor(s) to fix this."
                )

            # Group by (hour, day_of_week) and average
            hourly_values: dict[tuple[int, int], list[float]] = {}
            for ts_key, net_kwh in hourly_net.items():
                dt = dt_util.parse_datetime(ts_key)
                if dt is None:
                    continue
                key = (dt.hour, dt.weekday())
                hourly_values.setdefault(key, []).append(max(0.0, net_kwh))

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

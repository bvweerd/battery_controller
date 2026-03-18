"""Forecast coordinator for the Battery Controller integration."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ELECTRICITY_CONSUMPTION_SENSORS,
    CONF_ELECTRICITY_PRODUCTION_SENSORS,
    CONF_PV_PRODUCTION_SENSORS,
)
from .coordinator_weather import WeatherDataCoordinator
from .forecast_models import (
    ConsumptionForecastModel,
    NetLoadForecast,
    PVForecastModel,
)

_LOGGER = logging.getLogger(__name__)


class ForecastCoordinator(DataUpdateCoordinator):
    """Coordinator for PV and consumption forecasts."""

    def __init__(
        self,
        hass: HomeAssistant,
        weather_coordinator: WeatherDataCoordinator,
        config: dict[str, Any],
    ):
        """Initialize the forecast coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name="Battery Controller Forecast",
            update_interval=timedelta(minutes=15),
        )
        self.weather_coordinator = weather_coordinator
        self.config = config

        # Build AC and DC PV forecast models from subentry data.
        # config["pv_arrays"] is a list of subentry data dicts injected by async_setup_entry.
        # Each dict contains a "subentry_id" key for per-array forecast keying.
        self.pv_ac_models: list[PVForecastModel] = []
        self.pv_ac_subentry_ids: list[str] = []
        self.pv_dc_models: list[PVForecastModel] = []
        self.pv_dc_subentry_ids: list[str] = []
        for arr in config.get("pv_arrays", []):
            kwp = float(arr.get("peak_power_kwp", 0))
            if kwp <= 0:
                continue
            orientation = float(arr.get("orientation", 180.0))
            tilt = float(arr.get("tilt", 35.0))
            efficiency_factor = float(arr.get("efficiency_factor", 0.85))
            dc_coupled = bool(arr.get("dc_coupled", False))
            subentry_id = arr.get("subentry_id", "")
            if dc_coupled:
                # DC PV: raw panel output; DC coupling efficiency handled by battery model
                self.pv_dc_models.append(
                    PVForecastModel(
                        peak_power_kwp=kwp,
                        orientation_deg=orientation,
                        tilt_deg=tilt,
                        efficiency_factor=1.0,
                    )
                )
                self.pv_dc_subentry_ids.append(subentry_id)
            else:
                self.pv_ac_models.append(
                    PVForecastModel(
                        peak_power_kwp=kwp,
                        orientation_deg=orientation,
                        tilt_deg=tilt,
                        efficiency_factor=efficiency_factor,
                    )
                )
                self.pv_ac_subentry_ids.append(subentry_id)

        # Dummy zero-power PV model for NetLoadForecast (consumption only)
        _dummy_pv = PVForecastModel(
            peak_power_kwp=0.0, orientation_deg=180, tilt_deg=35, efficiency_factor=1.0
        )

        self.consumption_model = ConsumptionForecastModel(
            hass=hass,
            consumption_sensors=config.get(CONF_ELECTRICITY_CONSUMPTION_SENSORS, []),
            production_sensors=config.get(CONF_ELECTRICITY_PRODUCTION_SENSORS, []),
            history_days=14,
            base_consumption_kw=0.5,
            pv_production_sensors=config.get(CONF_PV_PRODUCTION_SENSORS, []),
            entry_id=config.get("entry_id"),
        )

        self.net_load_model = NetLoadForecast(
            pv_model=_dummy_pv,
            consumption_model=self.consumption_model,
        )

        # Unsubscribe handle for the daily pattern-refresh timer
        self._unsub_pattern_refresh: Any | None = None

    async def async_setup(self) -> None:
        """Set up the forecast coordinator."""
        # Update consumption pattern from history on startup
        await self.consumption_model.async_update_pattern()

        # Re-learn the consumption pattern every 24 h so that seasonal changes
        # and new appliances are picked up automatically without an integration
        # reload.
        self._unsub_pattern_refresh = async_track_time_interval(
            self.hass,
            self._handle_pattern_refresh,
            timedelta(hours=24),
        )

    async def _handle_pattern_refresh(self, now: datetime) -> None:
        """Refresh consumption pattern from HA recorder (daily timer)."""
        _LOGGER.debug("Daily consumption pattern refresh triggered at %s", now)
        await self.consumption_model.async_update_pattern()

    async def async_shutdown(self) -> None:
        """Clean up resources."""
        if self._unsub_pattern_refresh:
            self._unsub_pattern_refresh()
            self._unsub_pattern_refresh = None

    async def _async_update_data(self) -> dict[str, Any]:
        """Calculate PV and consumption forecasts."""
        weather_data = self.weather_coordinator.data
        if not weather_data:
            raise UpdateFailed("No weather data available")

        radiation_forecast = weather_data.get("radiation_forecast", [])
        wind_speed_forecast = weather_data.get("wind_speed_forecast", [])
        temperature_forecast = weather_data.get("temperature_forecast", [])
        forecast_start = weather_data.get("forecast_start_utc")
        if forecast_start and radiation_forecast:
            current_hour = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
            hours_elapsed = max(
                0, int((current_hour - forecast_start).total_seconds() / 3600)
            )
            if hours_elapsed > 0:
                radiation_forecast = radiation_forecast[hours_elapsed:]
                wind_speed_forecast = (
                    wind_speed_forecast[hours_elapsed:] if wind_speed_forecast else []
                )
                temperature_forecast = (
                    temperature_forecast[hours_elapsed:] if temperature_forecast else []
                )
                _LOGGER.debug(
                    "Radiation forecast shifted by %d hours (weather data age)",
                    hours_elapsed,
                )

        # Consumption forecast via net_load_model (dummy PV so result = pure consumption)
        _, consumption_forecast, _ = self.net_load_model.forecast(radiation_forecast)
        n = len(consumption_forecast)

        # Temperature forecast for PV derating (P2.2): pass if available
        temp_for_pv = temperature_forecast if temperature_forecast else None

        # Sum AC PV forecast across all AC subentry models, applying temperature derating
        pv_forecast = [0.0] * n
        per_pv_array_forecasts: dict[str, list[float]] = {}
        for sid, model in zip(self.pv_ac_subentry_ids, self.pv_ac_models):
            extra = model.forecast_from_radiation(radiation_forecast, temp_for_pv)
            arr_forecast = [round(max(0.0, v), 3) for v in extra[:n]]
            if sid:
                per_pv_array_forecasts[sid] = arr_forecast
            for i in range(min(n, len(extra))):
                pv_forecast[i] += extra[i]

        # Clamp PV values: a faulty sensor/model must not produce negative output (P1.3)
        pv_forecast = [max(0.0, v) for v in pv_forecast]

        net_load_forecast = [consumption_forecast[i] - pv_forecast[i] for i in range(n)]

        # Sum DC PV forecast across all DC subentry models, applying temperature derating
        has_dc = bool(self.pv_dc_models)
        pv_dc_forecast = [0.0] * n
        for sid, dc_model in zip(self.pv_dc_subentry_ids, self.pv_dc_models):
            extra_dc = dc_model.forecast_from_radiation(radiation_forecast, temp_for_pv)
            arr_forecast = [round(max(0.0, v), 3) for v in extra_dc[:n]]
            if sid:
                per_pv_array_forecasts[sid] = arr_forecast
            for i in range(min(n, len(extra_dc))):
                pv_dc_forecast[i] += extra_dc[i]

        # Clamp DC PV values as well
        pv_dc_forecast = [max(0.0, v) for v in pv_dc_forecast]

        # Derive current values from forecast (first element = current hour)
        current_pv = pv_forecast[0] if pv_forecast else 0.0
        current_dc_pv = pv_dc_forecast[0] if pv_dc_forecast else 0.0
        current_consumption = self.consumption_model.get_current_consumption()

        result = {
            "pv_forecast_kw": [round(v, 3) for v in pv_forecast],
            "pv_dc_forecast_kw": [round(v, 3) for v in pv_dc_forecast],
            "per_pv_array_forecasts": per_pv_array_forecasts,
            "consumption_forecast_kw": [round(v, 3) for v in consumption_forecast],
            "net_load_forecast_kw": [round(v, 3) for v in net_load_forecast],
            "current_pv_kw": round(current_pv, 3),
            "current_dc_pv_kw": round(current_dc_pv, 3),
            "current_consumption_kw": round(current_consumption, 3),
            "current_net_load_kw": round(current_consumption - current_pv, 3),
            "current_ghi_wm2": round(radiation_forecast[0], 1)
            if radiation_forecast
            else 0.0,
            "current_wind_speed_ms": round(wind_speed_forecast[0], 1)
            if wind_speed_forecast
            else 0.0,
            "pv_dc_coupled": has_dc,
            "timestamp": dt_util.utcnow(),
        }

        _LOGGER.debug(
            "Forecast updated: AC_PV=%.2f kW, DC_PV=%.2f kW, consumption=%.2f kW",
            current_pv,
            current_dc_pv,
            current_consumption,
        )

        return result

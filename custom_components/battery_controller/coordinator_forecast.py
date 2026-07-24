"""Forecast coordinator for the Battery Controller integration."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    CONF_ELECTRICITY_CONSUMPTION_SENSORS,
    CONF_ELECTRICITY_PRODUCTION_SENSORS,
    CONF_PV_PRODUCTION_SENSORS,
    DOMAIN,
    FORECAST_INTERVAL_MINUTES,
    WEATHER_STALE_AFTER_MINUTES,
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
        # Unsubscribe handle for the weather-coordinator listener
        self._unsub_weather: Any | None = None

    async def async_setup(self) -> None:
        """Set up the forecast coordinator."""
        # Update consumption pattern from history on startup
        await self.consumption_model.async_update_pattern()

        # Subscribe to the weather coordinator. A DataUpdateCoordinator only
        # keeps polling while it has listeners, and no entity subscribes to the
        # weather coordinator directly — without this listener it would fetch
        # exactly once at startup and then serve the same (aging) snapshot
        # forever. The listener also recomputes the forecasts as soon as fresh
        # weather data arrives instead of waiting for the next 15-min cycle.
        @callback
        def _on_weather_update() -> None:
            self.hass.async_create_task(self.async_request_refresh())

        self._unsub_weather = self.weather_coordinator.async_add_listener(
            _on_weather_update
        )

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
        if self._unsub_weather:
            self._unsub_weather()
            self._unsub_weather = None
        await super().async_shutdown()

    async def _async_update_data(self) -> dict[str, Any]:
        """Calculate PV and consumption forecasts."""
        weather_data = self.weather_coordinator.data
        if not weather_data:
            _LOGGER.warning(
                "Weather data unavailable; using zero radiation fallback "
                "(PV forecast = 0, consumption forecast still active)"
            )
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                "weather_data_unavailable",
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="weather_data_unavailable",
            )
            radiation_forecast: list[float] = [0.0] * 48
            dni_forecast: list[float] = []
            diffuse_forecast: list[float] = []
            wind_speed_forecast: list[float] = []
            temperature_forecast: list[float] = []
            forecast_start = None
            ir.async_delete_issue(self.hass, DOMAIN, "weather_data_stale")
        else:
            ir.async_delete_issue(self.hass, DOMAIN, "weather_data_unavailable")
            # Stale weather detection: the shifted forecast degrades gracefully,
            # but the user should know the weather source has stopped updating.
            weather_ts = weather_data.get("timestamp")
            weather_age_min = (
                (dt_util.utcnow() - weather_ts).total_seconds() / 60
                if weather_ts is not None
                else None
            )
            if (
                weather_age_min is not None
                and weather_age_min > WEATHER_STALE_AFTER_MINUTES
            ):
                _LOGGER.warning(
                    "Weather data is stale (%.0f min old, limit %.0f min); "
                    "PV forecast quality degrades until open-meteo updates resume",
                    weather_age_min,
                    WEATHER_STALE_AFTER_MINUTES,
                )
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    "weather_data_stale",
                    is_fixable=False,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key="weather_data_stale",
                    # Whole hours: keeps issue-registry rewrites to once per hour
                    translation_placeholders={
                        "age_hours": f"{weather_age_min / 60:.0f}"
                    },
                )
            else:
                ir.async_delete_issue(self.hass, DOMAIN, "weather_data_stale")
            radiation_forecast = weather_data.get("radiation_forecast", [])
            dni_forecast = weather_data.get("dni_forecast", [])
            diffuse_forecast = weather_data.get("diffuse_forecast", [])
            wind_speed_forecast = weather_data.get("wind_speed_forecast", [])
            temperature_forecast = weather_data.get("temperature_forecast", [])
            forecast_start = weather_data.get("forecast_start_utc")
        current_hour = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
        if forecast_start and radiation_forecast:
            hours_elapsed = max(
                0, int((current_hour - forecast_start).total_seconds() / 3600)
            )
            if hours_elapsed > 0:
                radiation_forecast = radiation_forecast[hours_elapsed:]
                dni_forecast = dni_forecast[hours_elapsed:] if dni_forecast else []
                diffuse_forecast = (
                    diffuse_forecast[hours_elapsed:] if diffuse_forecast else []
                )
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

        # Emit all forecasts at FORECAST_INTERVAL_MINUTES resolution, aligned
        # to the current step boundary (quarter hour). Hourly weather series
        # are expanded by repetition — mean-preserving, unlike interpolation —
        # while solar geometry is evaluated per step, giving dawn/dusk ramps
        # sub-hourly shape. Starting at the current step (not the current
        # hour) keeps step k aligned with price period k for sub-hourly
        # price intervals.
        step_min = FORECAST_INTERVAL_MINUTES
        steps_per_hour = 60 // step_min
        now_utc = dt_util.utcnow()
        current_step = now_utc.replace(
            minute=(now_utc.minute // step_min) * step_min, second=0, microsecond=0
        )
        offset_steps = int(
            (current_step - current_hour).total_seconds() // (step_min * 60)
        )
        n_hours = len(radiation_forecast)
        n = max(0, n_hours * steps_per_hour - offset_steps)

        # Build UTC timestamps for each forecast step (needed for solar geometry)
        lat = self.hass.config.latitude
        lon = self.hass.config.longitude
        timestamps_utc = [
            current_step + timedelta(minutes=step_min * k) for k in range(n)
        ]

        def _expand(hourly: list[float]) -> list[float]:
            """Expand an hourly series (index 0 = current hour) to step values."""
            if not hourly:
                return []
            out: list[float] = []
            for ts in timestamps_utc:
                idx = int((ts - current_hour).total_seconds() // 3600)
                out.append(hourly[idx] if 0 <= idx < len(hourly) else 0.0)
            return out

        radiation_steps = _expand(radiation_forecast)
        dni_steps = _expand(dni_forecast)
        diffuse_steps = _expand(diffuse_forecast)
        wind_speed_steps = _expand(wind_speed_forecast)
        temperature_steps = _expand(temperature_forecast)

        # Consumption forecast via net_load_model (dummy PV so result = pure
        # consumption); the model works in hourly buckets, expanded to steps.
        _, consumption_hourly, _ = self.net_load_model.forecast(radiation_forecast)
        consumption_forecast = _expand(consumption_hourly)

        # Temperature forecast for PV derating (P2.2): pass if available
        temp_for_pv = temperature_steps if temperature_steps else None

        poa_dni: list[float] | None = dni_steps or None
        poa_diffuse: list[float] | None = diffuse_steps or None

        # Sum AC PV forecast across all AC subentry models, applying temperature derating
        pv_forecast = [0.0] * n
        per_pv_array_forecasts: dict[str, list[float]] = {}
        for sid, model in zip(self.pv_ac_subentry_ids, self.pv_ac_models):
            extra = model.forecast_from_radiation(
                radiation_steps,
                temp_for_pv,
                dni_forecast=poa_dni,
                diffuse_forecast=poa_diffuse,
                timestamps_utc=timestamps_utc,
                latitude=lat,
                longitude=lon,
            )
            arr_forecast = [round(max(0.0, v), 3) for v in extra[:n]]
            if sid:
                per_pv_array_forecasts[sid] = arr_forecast
            for i in range(min(n, len(extra))):
                pv_forecast[i] += extra[i]

        # Clamp PV values: a faulty sensor/model must not produce negative output (P1.3)
        pv_forecast = [max(0.0, v) for v in pv_forecast]

        net_load_forecast = [c - p for c, p in zip(consumption_forecast, pv_forecast)]

        # Sum DC PV forecast across all DC subentry models, applying temperature derating
        has_dc = bool(self.pv_dc_models)
        pv_dc_forecast = [0.0] * n
        for sid, dc_model in zip(self.pv_dc_subentry_ids, self.pv_dc_models):
            extra_dc = dc_model.forecast_from_radiation(
                radiation_steps,
                temp_for_pv,
                dni_forecast=poa_dni,
                diffuse_forecast=poa_diffuse,
                timestamps_utc=timestamps_utc,
                latitude=lat,
                longitude=lon,
            )
            arr_forecast = [round(max(0.0, v), 3) for v in extra_dc[:n]]
            if sid:
                per_pv_array_forecasts[sid] = arr_forecast
            for i in range(min(n, len(extra_dc))):
                pv_dc_forecast[i] += extra_dc[i]

        # Clamp DC PV values as well
        pv_dc_forecast = [max(0.0, v) for v in pv_dc_forecast]

        # Derive current values from forecast (first element = current step)
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
            "current_ghi_wm2": round(radiation_steps[0], 1) if radiation_steps else 0.0,
            "current_wind_speed_ms": round(wind_speed_steps[0], 1)
            if wind_speed_steps
            else 0.0,
            "pv_dc_coupled": has_dc,
            "forecast_interval_minutes": step_min,
            "forecast_start_utc": current_step,
            "timestamp": dt_util.utcnow(),
        }

        _LOGGER.debug(
            "Forecast updated: AC_PV=%.2f kW, DC_PV=%.2f kW, consumption=%.2f kW",
            current_pv,
            current_dc_pv,
            current_consumption,
        )

        return result

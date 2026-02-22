"""Data update coordinators for the Battery Controller integration."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant, Event, EventStateChangedData, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.util import dt as dt_util

from .battery_model import BatteryConfig, BatteryState
from .const import (
    CONF_BATTERY_SOC_SENSOR,
    CONF_BATTERY_POWER_SENSOR,
    CONF_CONTROL_MODE,
    CONF_DEGRADATION_COST_PER_KWH,
    CONF_ELECTRICITY_CONSUMPTION_SENSORS,
    CONF_ELECTRICITY_PRODUCTION_SENSORS,
    CONF_FEED_IN_PRICE_SENSOR,
    CONF_FIXED_FEED_IN_PRICE,
    CONF_MIN_PRICE_SPREAD,
    CONF_OPTIMIZATION_INTERVAL_MINUTES,
    CONF_POWER_CONSUMPTION_SENSORS,
    CONF_POWER_PRODUCTION_SENSORS,
    CONF_PRICE_SENSOR,
    CONF_PV_PRODUCTION_SENSORS,
    CONF_PV_DC_COUPLED,
    CONF_PV_DC_PEAK_POWER_KWP,
    CONF_PV_EFFICIENCY_FACTOR,
    CONF_PV_EXTRA_ARRAYS,
    CONF_PV_ORIENTATION,
    CONF_PV_PEAK_POWER_KWP,
    CONF_PV_TILT,
    CONF_TIME_STEP_MINUTES,
    DEFAULT_CONTROL_MODE,
    DEFAULT_DEGRADATION_COST_PER_KWH,
    DEFAULT_FIXED_FEED_IN_PRICE,
    DEFAULT_MIN_PRICE_SPREAD,
    DEFAULT_OPTIMIZATION_INTERVAL_MINUTES,
    DEFAULT_PV_DC_COUPLED,
    DEFAULT_PV_DC_PEAK_POWER_KWP,
    DEFAULT_PV_EFFICIENCY_FACTOR,
    DEFAULT_PV_ORIENTATION,
    DEFAULT_PV_PEAK_POWER_KWP,
    DEFAULT_PV_TILT,
    DEFAULT_TIME_STEP_MINUTES,
    CONF_ZERO_GRID_RESPONSE_TIME_S,
    DEFAULT_ZERO_GRID_RESPONSE_TIME_S,
    MODE_HYBRID,
    MODE_MANUAL,
    MODE_ZERO_GRID,
)
from .forecast_models import (
    ConsumptionForecastModel,
    NetLoadForecast,
    PriceForecastModel,
    PVForecastModel,
)
from .helpers import (
    extract_price_forecast_with_interval,
    get_sensor_value,
    resample_forecast,
)
from .optimizer import optimize_battery_schedule, OptimizationResult
from .zero_grid_controller import create_zero_grid_controller

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

        url = (
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude={self.latitude}&longitude={self.longitude}"
            "&hourly=temperature_2m,shortwave_radiation,wind_speed_10m"
            "&current_weather=true&timezone=UTC&forecast_days=2"
        )

        try:
            async with self.session.get(
                url, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    raise UpdateFailed(f"API returned status {resp.status}")
                data = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise UpdateFailed(f"Error fetching weather data: {err}")

        # Extract hourly forecasts
        hourly = data.get("hourly", {})
        times = hourly.get("time", [])
        radiation = hourly.get("shortwave_radiation", [])
        wind_speed = hourly.get("wind_speed_10m", [])

        if not times or not radiation:
            raise UpdateFailed("No forecast data in API response")

        # Find current hour index
        now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        start_idx = 0
        for i, ts in enumerate(times):
            try:
                t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                if t.tzinfo is None:
                    t = t.replace(tzinfo=timezone.utc)
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

        result = {
            "radiation_forecast": [round(v, 1) for v in radiation_forecast],
            "wind_speed_forecast": [round(v, 1) for v in wind_speed_forecast],
            "forecast_start_utc": now,
            "timestamp": dt_util.utcnow(),
        }

        _LOGGER.debug(
            "Weather data updated: %d hours of radiation/wind forecast",
            len(radiation_forecast),
        )

        return result


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

        # Initialize forecast models - AC PV arrays
        efficiency = float(
            config.get(CONF_PV_EFFICIENCY_FACTOR, DEFAULT_PV_EFFICIENCY_FACTOR)
        )

        # Primary AC PV array
        self.pv_model = PVForecastModel(
            peak_power_kwp=float(
                config.get(CONF_PV_PEAK_POWER_KWP, DEFAULT_PV_PEAK_POWER_KWP)
            ),
            orientation_deg=float(
                config.get(CONF_PV_ORIENTATION, DEFAULT_PV_ORIENTATION)
            ),
            tilt_deg=float(config.get(CONF_PV_TILT, DEFAULT_PV_TILT)),
            efficiency_factor=efficiency,
        )

        # Additional PV arrays from dynamic list (AC and DC-coupled)
        self.pv_extra_models: list[PVForecastModel] = []
        self.pv_extra_dc_models: list[PVForecastModel] = []
        for arr in config.get(CONF_PV_EXTRA_ARRAYS, []):
            kwp = float(arr.get("peak_power_kwp", 0))
            if kwp <= 0:
                continue
            orientation = float(arr.get("orientation", 180))
            tilt = float(arr.get("tilt", 35))
            dc_coupled = bool(arr.get("dc_coupled", False))
            if dc_coupled:
                self.pv_extra_dc_models.append(
                    PVForecastModel(
                        peak_power_kwp=kwp,
                        orientation_deg=orientation,
                        tilt_deg=tilt,
                        # DC PV uses raw panel efficiency (no inverter loss)
                        efficiency_factor=1.0,
                    )
                )
            else:
                self.pv_extra_models.append(
                    PVForecastModel(
                        peak_power_kwp=kwp,
                        orientation_deg=orientation,
                        tilt_deg=tilt,
                        efficiency_factor=efficiency,
                    )
                )

        # DC-coupled PV model for primary array (panels on battery inverter)
        # Uses same orientation/tilt as primary but different peak power and efficiency
        self.pv_dc_coupled = bool(config.get(CONF_PV_DC_COUPLED, DEFAULT_PV_DC_COUPLED))
        self.pv_dc_model = PVForecastModel(
            peak_power_kwp=float(
                config.get(CONF_PV_DC_PEAK_POWER_KWP, DEFAULT_PV_DC_PEAK_POWER_KWP)
            ),
            orientation_deg=float(
                config.get(CONF_PV_ORIENTATION, DEFAULT_PV_ORIENTATION)
            ),
            tilt_deg=float(config.get(CONF_PV_TILT, DEFAULT_PV_TILT)),
            # DC PV uses raw panel efficiency (no inverter loss on PV side)
            # The DC coupling efficiency is handled in the battery model
            efficiency_factor=1.0,
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
            pv_model=self.pv_model,
            consumption_model=self.consumption_model,
        )

    async def async_setup(self) -> None:
        """Set up the forecast coordinator."""
        # Update consumption pattern from history
        await self.consumption_model.async_update_pattern()

    async def async_shutdown(self) -> None:
        """Clean up resources."""
        pass

    async def _async_update_data(self) -> dict[str, Any]:
        """Calculate PV and consumption forecasts."""
        weather_data = self.weather_coordinator.data
        if not weather_data:
            raise UpdateFailed("No weather data available")

        radiation_forecast = weather_data.get("radiation_forecast", [])
        wind_speed_forecast = weather_data.get("wind_speed_forecast", [])
        forecast_start = weather_data.get("forecast_start_utc")
        if forecast_start and radiation_forecast:
            current_hour = datetime.now(timezone.utc).replace(
                minute=0, second=0, microsecond=0
            )
            hours_elapsed = max(
                0, int((current_hour - forecast_start).total_seconds() / 3600)
            )
            if hours_elapsed > 0:
                radiation_forecast = radiation_forecast[hours_elapsed:]
                wind_speed_forecast = (
                    wind_speed_forecast[hours_elapsed:] if wind_speed_forecast else []
                )
                _LOGGER.debug(
                    "Radiation forecast shifted by %d hours (weather data age)",
                    hours_elapsed,
                )

        # Generate AC PV and consumption forecasts (primary array)
        pv_forecast, consumption_forecast, net_load_forecast = (
            self.net_load_model.forecast(radiation_forecast)
        )

        # Add production from extra PV arrays
        for extra_model in self.pv_extra_models:
            extra_forecast = extra_model.forecast_from_radiation(radiation_forecast)
            for i in range(min(len(pv_forecast), len(extra_forecast))):
                pv_forecast[i] += extra_forecast[i]
                net_load_forecast[i] -= extra_forecast[i]

        # Generate DC-coupled PV forecast (primary DC + extra DC arrays)
        pv_dc_forecast = [0.0] * len(pv_forecast)
        current_dc_pv = 0.0
        has_dc = self.pv_dc_coupled and self.pv_dc_model.peak_power_kwp > 0
        if has_dc:
            pv_dc_forecast = self.pv_dc_model.forecast_from_radiation(
                radiation_forecast
            )
            while len(pv_dc_forecast) < len(pv_forecast):
                pv_dc_forecast.append(0.0)

        # Add production from extra DC-coupled arrays
        for dc_model in self.pv_extra_dc_models:
            has_dc = True
            extra_dc = dc_model.forecast_from_radiation(radiation_forecast)
            for i in range(min(len(pv_dc_forecast), len(extra_dc))):
                pv_dc_forecast[i] += extra_dc[i]

        # Derive current values from forecast (first element = current hour)
        current_pv = pv_forecast[0] if pv_forecast else 0.0
        current_dc_pv = pv_dc_forecast[0] if pv_dc_forecast else 0.0
        current_consumption = self.consumption_model.get_current_consumption()

        result = {
            "pv_forecast_kw": [round(v, 3) for v in pv_forecast],
            "pv_dc_forecast_kw": [round(v, 3) for v in pv_dc_forecast],
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


class OptimizationCoordinator(DataUpdateCoordinator):
    """Coordinator for battery optimization."""

    def __init__(
        self,
        hass: HomeAssistant,
        weather_coordinator: WeatherDataCoordinator,
        forecast_coordinator: ForecastCoordinator,
        config: dict[str, Any],
    ):
        """Initialize the optimization coordinator."""
        interval_minutes = int(
            config.get(
                CONF_OPTIMIZATION_INTERVAL_MINUTES,
                DEFAULT_OPTIMIZATION_INTERVAL_MINUTES,
            )
        )

        super().__init__(
            hass,
            _LOGGER,
            name="Battery Controller Optimization",
            update_interval=timedelta(minutes=interval_minutes),
        )

        self.weather_coordinator = weather_coordinator
        self.forecast_coordinator = forecast_coordinator
        self.config = config

        # Battery configuration
        self.battery_config = BatteryConfig.from_config(config)

        # Zero-grid controller
        self.zero_grid_controller = create_zero_grid_controller(
            config, self.battery_config
        )

        # Control mode (restore from config or use default)
        self._control_mode = config.get(CONF_CONTROL_MODE, DEFAULT_CONTROL_MODE)

        # Price sensor tracking
        self._price_sensor = config.get(CONF_PRICE_SENSOR)
        self._unsub_price: Any | None = None
        self._last_price: float | None = None

        # Real-time sensors for zero_grid control
        self._battery_power_sensor = config.get(CONF_BATTERY_POWER_SENSOR)
        self._battery_soc_sensor = config.get(CONF_BATTERY_SOC_SENSOR)
        self._power_consumption_sensors = config.get(CONF_POWER_CONSUMPTION_SENSORS, [])
        self._power_production_sensors = config.get(CONF_POWER_PRODUCTION_SENSORS, [])
        self._unsub_realtime: Any | None = None

        # Last optimization result and effective mode (persists between real-time updates)
        self._last_result: OptimizationResult | None = None
        self._effective_mode: str = "idle"
        self._effective_power: float = 0.0
        self._dp_schedule_w: float = 0.0

        # Failure tracking and cascade listeners
        self._last_failure_reason: str | None = None
        self._last_success_time: datetime | None = None
        self._unsub_soc: Any | None = None
        self._unsub_forecast: Any | None = None
        self._unsub_optimizer_timer: Any | None = None
        self._interval_minutes: int = interval_minutes

        # Historical price forecast model (fallback when day-ahead not yet published)
        self._price_model = PriceForecastModel(
            hass=hass,
            price_sensor_id=config.get(CONF_PRICE_SENSOR, ""),
            entry_id=config.get("entry_id"),
            history_days=30,
        )

        # Enabled flag: when False _async_update_data returns cached data immediately
        # without re-running the optimizer. The 15-min scheduler keeps running so it
        # is trivial to re-enable without manual intervention.
        self._optimization_enabled: bool = True

    @property
    def control_mode(self) -> str:
        """Get current control mode."""
        return self._control_mode

    @control_mode.setter
    def control_mode(self, mode: str) -> None:
        """Set control mode."""
        self._control_mode = mode

    @property
    def last_failure_reason(self) -> str | None:
        """Return the reason for the last failed update, or None if last update succeeded."""
        return self._last_failure_reason

    @property
    def last_success_time(self) -> datetime | None:
        """Return the UTC timestamp of the last successful optimization, or None."""
        return self._last_success_time

    @property
    def optimization_enabled(self) -> bool:
        """Return whether the optimizer is enabled."""
        return self._optimization_enabled

    @optimization_enabled.setter
    def optimization_enabled(self, value: bool) -> None:
        """Enable or disable the optimizer."""
        self._optimization_enabled = value

    async def async_setup(self) -> None:
        """Set up event tracking for price changes and real-time control."""
        await self._price_model.async_update_pattern()

        if self._price_sensor:
            self._unsub_price = async_track_state_change_event(
                self.hass,
                [self._price_sensor],
                self._handle_price_change,
            )
            _LOGGER.debug("Tracking price sensor: %s", self._price_sensor)

        if self._battery_soc_sensor:
            self._unsub_soc = async_track_state_change_event(
                self.hass,
                [self._battery_soc_sensor],
                self._handle_soc_available,
            )
            _LOGGER.debug("Tracking SoC sensor: %s", self._battery_soc_sensor)

        @callback
        def _on_forecast_update() -> None:
            """Trigger optimization when forecast data first becomes available."""
            if self.forecast_coordinator.data is not None and self.data is None:
                self.hass.async_create_task(self.async_request_refresh())

        self._unsub_forecast = self.forecast_coordinator.async_add_listener(
            _on_forecast_update
        )

        # Guaranteed periodic timer using async_track_time_interval.
        # DataUpdateCoordinator's own update_interval only reschedules when
        # listeners are registered — which doesn't happen until platform entities
        # call async_added_to_hass(). If the first refresh fails before entities
        # register (common at HA startup when input sensors are unavailable) the
        # coordinator's internal timer is never created and the optimizer never
        # runs again. This timer fires unconditionally, bypassing that mechanism.
        self._unsub_optimizer_timer = async_track_time_interval(
            self.hass,
            self._handle_optimization_interval,
            timedelta(minutes=self._interval_minutes),
        )
        _LOGGER.debug(
            "Optimization interval timer started: every %d minutes",
            self._interval_minutes,
        )

        # Set up real-time zero_grid control via a periodic timer.
        # A timer avoids the double-trigger problem that occurs when multiple
        # sensors (e.g. DSMR consumption + production) update simultaneously:
        # with state-change tracking each sensor fires a separate event,
        # causing the zero_grid integrator to run twice in rapid succession
        # and double the setpoint. A fixed interval reads all sensors at once.
        has_power_sensors = bool(
            self._power_consumption_sensors or self._power_production_sensors
        )
        if has_power_sensors:
            interval_s = float(
                self.config.get(
                    CONF_ZERO_GRID_RESPONSE_TIME_S, DEFAULT_ZERO_GRID_RESPONSE_TIME_S
                )
            )
            self._unsub_realtime = async_track_time_interval(
                self.hass,
                self._handle_realtime_update,
                timedelta(seconds=interval_s),
            )
            _LOGGER.debug(
                "Real-time zero_grid control enabled, interval=%.1fs, sensors: %s",
                interval_s,
                self._power_consumption_sensors + self._power_production_sensors,
            )

    async def _handle_price_change(self, event: Event[EventStateChangedData]) -> None:
        """Handle price sensor state changes.

        Triggers optimization when:
        - The sensor becomes available for the first time (e.g. after HA restart)
        - The price changes significantly (>10%)
        """
        new_state = event.data.get("new_state")
        if not new_state:
            return

        try:
            new_price = float(new_state.state)
        except (ValueError, TypeError):
            return  # Sensor is unavailable/unknown, ignore

        old_state = event.data.get("old_state")
        was_unavailable = (
            self._last_price is None
            or old_state is None
            or old_state.state in ("unknown", "unavailable")
        )

        if was_unavailable:
            # Sensor just became available — trigger a full optimization refresh
            _LOGGER.debug(
                "Price sensor '%s' became available (%.4f), triggering optimization",
                self._price_sensor,
                new_price,
            )
            self._last_price = new_price
            await self.async_request_refresh()
        elif self._last_price != 0:
            change_pct = abs(new_price - self._last_price) / abs(self._last_price)
            if change_pct >= 0.10:
                _LOGGER.debug(
                    "Significant price change: %.2f%%, triggering optimization",
                    change_pct * 100,
                )
                await self.async_request_refresh()
            self._last_price = new_price

    async def _handle_optimization_interval(self, now: datetime) -> None:
        """Periodic optimization trigger via async_track_time_interval.

        This fires unconditionally every interval_minutes, independent of whether
        DataUpdateCoordinator has any listeners registered.  It is the primary
        scheduling mechanism; the coordinator's own update_interval is kept as a
        fallback so that HA's built-in retry / backoff logic still applies.
        """
        _LOGGER.debug("Optimization interval timer fired at %s", now)
        await self.async_request_refresh()

    async def _handle_soc_available(self, event: Event[EventStateChangedData]) -> None:
        """Trigger refresh when SoC sensor transitions from unavailable to available."""
        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")
        was_unavailable = old_state is None or old_state.state in (
            "unknown",
            "unavailable",
        )
        is_available = new_state is not None and new_state.state not in (
            "unknown",
            "unavailable",
        )
        if was_unavailable and is_available:
            _LOGGER.debug(
                "SoC sensor '%s' became available, triggering optimization",
                self._battery_soc_sensor,
            )
            await self.async_request_refresh()

    async def _handle_realtime_update(self, now: datetime) -> None:
        """Periodic real-time update for zero_grid control.

        Runs every CONF_ZERO_GRID_RESPONSE_TIME_S seconds and recalculates
        the zero_grid setpoint from current sensor values. Using a timer
        instead of state-change events avoids double-triggers when multiple
        sensors (e.g. DSMR consumption + production) update simultaneously.
        """
        if self.data is None or self._last_result is None:
            return  # No optimization result yet

        # Read actual grid power from DSMR sensor
        current_grid_w = self._get_realtime_grid_w()
        if current_grid_w is None:
            return

        # Read current battery state
        battery_state = self.get_current_battery_state()

        controller_mode = self._resolve_controller_mode(
            self._effective_mode, current_grid_w
        )

        # Recalculate zero_grid setpoint with actual sensor data
        control_action = self.zero_grid_controller.get_control_action(
            current_grid_w=current_grid_w,
            current_soc_kwh=battery_state.soc_kwh,
            current_battery_w=battery_state.power_kw * 1000,
            dp_schedule_w=self._dp_schedule_w,
            mode=controller_mode,
        )

        # Update coordinator data with new control action (triggers sensor updates)
        self.async_set_updated_data(
            {
                **self.data,
                "control_action": control_action,
                "battery_state": battery_state,
                "optimal_power_kw": control_action["target_power_kw"],
                "optimal_mode": control_action["action_mode"],
            }
        )

    def _resolve_controller_mode(
        self, effective_mode: str, current_grid_w: float
    ) -> str:
        """Map effective mode to zero_grid_controller mode.

        For idle mode with PV surplus (grid < 0), upgrades to zero_grid
        when real-time power sensors are available. Uses a 50 W hysteresis:
        enter zero_grid when grid < 0, stay in zero_grid until grid >= 50 W.
        This prevents oscillation when the battery successfully absorbs PV and
        grid reads near 0 W (which would otherwise flip back to idle mode,
        stopping the charge, causing the grid to go negative again).

        Args:
            effective_mode: The resolved mode from optimization logic.
            current_grid_w: Current grid power in W (positive = import).

        Returns:
            Controller mode string for ZeroGridController.
        """
        has_power_sensors = bool(
            self._power_consumption_sensors or self._power_production_sensors
        )

        if effective_mode == "zero_grid":
            return "zero_grid"
        deadband_w = self.zero_grid_controller.config.deadband_w
        if (
            effective_mode == "idle"
            and current_grid_w < deadband_w
            and has_power_sensors
        ):
            return "zero_grid"
        if effective_mode == "idle":
            return "idle"
        if effective_mode == "manual":
            return "manual"
        if effective_mode in ("charging", "discharging"):
            return "follow_schedule"
        return self._control_mode

    def _get_realtime_grid_w(self) -> float | None:
        """Read current grid power from DSMR power sensors.

        Calculates grid power as: sum(consumption) - sum(production).

        Note: DSMR sensors already include battery power in their readings:
        - consumption = household + battery_charging
        - production = PV - battery_discharging (or + depending on config)
        So the result already reflects the net grid flow including battery impact.
        We don't need to subtract battery_power separately.

        Returns:
            Grid power in W (positive = import), or None if no sensors configured.
        """
        if not (self._power_consumption_sensors or self._power_production_sensors):
            return None

        total_consumption = 0.0
        total_production = 0.0

        # Sum all consumption sensors
        for sensor_id in self._power_consumption_sensors:
            state = self.hass.states.get(sensor_id)
            if state and state.state not in ("unknown", "unavailable"):
                try:
                    value = float(state.state)
                    # Check unit: if in kW, convert to W
                    unit = state.attributes.get("unit_of_measurement", "W")
                    if unit == "kW":
                        value *= 1000
                    total_consumption += value
                except (ValueError, TypeError):
                    pass

        # Sum all production sensors
        for sensor_id in self._power_production_sensors:
            state = self.hass.states.get(sensor_id)
            if state and state.state not in ("unknown", "unavailable"):
                try:
                    value = float(state.state)
                    # Check unit: if in kW, convert to W
                    unit = state.attributes.get("unit_of_measurement", "W")
                    if unit == "kW":
                        value *= 1000
                    total_production += value
                except (ValueError, TypeError):
                    pass

        return total_consumption - total_production

    async def async_shutdown(self) -> None:
        """Clean up event tracking."""
        if self._unsub_price:
            self._unsub_price()
            self._unsub_price = None
        if self._unsub_soc:
            self._unsub_soc()
            self._unsub_soc = None
        if self._unsub_forecast:
            self._unsub_forecast()
            self._unsub_forecast = None
        if self._unsub_optimizer_timer:
            self._unsub_optimizer_timer()
            self._unsub_optimizer_timer = None
        if self._unsub_realtime:
            self._unsub_realtime()
            self._unsub_realtime = None

    def get_current_battery_state(self) -> BatteryState:
        """Get current battery state from sensors."""
        soc_sensor = self.config.get(CONF_BATTERY_SOC_SENSOR)
        power_sensor = self.config.get(CONF_BATTERY_POWER_SENSOR)

        # Determine a smarter default for soc_value: last known SoC, otherwise 50.0
        smarter_soc_default = 50.0
        if self.data and self.data.get("battery_state"):
            smarter_soc_default = self.data["battery_state"].soc_percent
        soc_value = get_sensor_value(self.hass, soc_sensor, smarter_soc_default)
        power_value = get_sensor_value(self.hass, power_sensor, 0.0)

        # Determine if SoC is in percent or kWh
        if soc_sensor:
            state = self.hass.states.get(soc_sensor)
            if state and state.state not in ("unknown", "unavailable"):
                unit = state.attributes.get("unit_of_measurement", "")
                if unit == "kWh":
                    soc_kwh = soc_value
                    soc_percent = (soc_kwh / self.battery_config.capacity_kwh) * 100
                else:
                    soc_percent = soc_value
                    soc_kwh = (soc_percent / 100) * self.battery_config.capacity_kwh
            else:
                # Sensor unavailable/not yet loaded — use default
                soc_percent = smarter_soc_default
                soc_kwh = (soc_percent / 100) * self.battery_config.capacity_kwh
                _LOGGER.debug(
                    "SoC sensor unavailable, using fallback SoC=%.1f%%", soc_percent
                )
        else:
            soc_percent = soc_value
            soc_kwh = (soc_percent / 100) * self.battery_config.capacity_kwh

        # Convert power to kW (check unit, default to W)
        power_kw = power_value
        if power_sensor:
            state = self.hass.states.get(power_sensor)
            if state:
                unit = state.attributes.get("unit_of_measurement", "W")
                if unit == "W":
                    power_kw = power_value / 1000
                # else: already in kW

        # Determine mode from power (in W for comparison)
        power_w = power_kw * 1000
        if power_w > 50:
            mode = "charging"
        elif power_w < -50:
            mode = "discharging"
        else:
            mode = "idle"

        return BatteryState(
            soc_kwh=soc_kwh,
            soc_percent=soc_percent,
            power_kw=power_kw,
            mode=mode,
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Run battery optimization."""
        _LOGGER.debug("OptimizationCoordinator: _async_update_data started.")
        # When disabled via switch, skip re-running the optimizer but keep the
        # 15-minute scheduler alive so re-enabling resumes without any manual nudge.
        if not self._optimization_enabled:
            _LOGGER.debug(
                "OptimizationCoordinator: Optimization disabled, returning cached data."
            )
            if self.data is not None:
                return self.data

        # First run before any data exists: fall through to normal path so we
        # get valid initial data even when starting in the disabled state.
        _LOGGER.debug("OptimizationCoordinator: Fetching forecast data.")
        # Get forecast data
        forecast_data = self.forecast_coordinator.data
        _LOGGER.debug(
            "OptimizationCoordinator: Forecast data fetched (available: %s).",
            forecast_data is not None,
        )
        if not forecast_data:
            _LOGGER.error(
                "OptimizationCoordinator: Forecast data is not available. Cannot run optimization."
            )
            self._last_failure_reason = "No forecast data available"
            raise UpdateFailed("No forecast data available", retry_after=60)

        # Get price forecast
        if not self._price_sensor:
            _LOGGER.error(
                "OptimizationCoordinator: No price sensor configured. Cannot run optimization."
            )
            self._last_failure_reason = "No price sensor configured"
            raise UpdateFailed("No price sensor configured")

        _LOGGER.debug(
            "OptimizationCoordinator: Fetching price sensor state for %s.",
            self._price_sensor,
        )
        price_state = self.hass.states.get(self._price_sensor)
        price_forecast: list[float] = []
        price_interval: int = 60
        price_forecast_source: str = "live"

        sensor_ok = price_state is not None and price_state.state not in (
            "unknown",
            "unavailable",
        )
        _LOGGER.debug(
            "OptimizationCoordinator: Price sensor state fetched (available: %s).",
            sensor_ok,
        )

        if sensor_ok:
            price_forecast, price_interval = extract_price_forecast_with_interval(
                price_state
            )

        if not price_forecast:
            # Sensor unavailable or has no forecast attributes: try historical model
            if self._price_model.has_data():
                weather_data = self.weather_coordinator.data or {}
                price_forecast = self._price_model.forecast(
                    hours=24,
                    ghi_forecast=weather_data.get("radiation_forecast"),
                    wind_forecast=weather_data.get("wind_speed_forecast"),
                )
                price_interval = 60
                price_forecast_source = "historical_model"
                _LOGGER.info(
                    "Using historical price model as fallback (price sensor %s)",
                    "unavailable" if not sensor_ok else "has no forecast",
                )
            elif sensor_ok:
                # No model data yet; fall back to current price as single value
                try:
                    price_forecast = [float(price_state.state)]
                    price_interval = 60
                    price_forecast_source = "current_only"
                except (ValueError, TypeError) as e:
                    _LOGGER.error(
                        "OptimizationCoordinator: Cannot extract numeric price data "
                        "from sensor '%s' (state: %s). Error: %s",
                        self._price_sensor,
                        price_state.state,
                        e,
                    )
                    self._last_failure_reason = (
                        f"Cannot extract price data from '{self._price_sensor}'"
                    )
                    raise UpdateFailed(
                        f"Cannot extract price data from '{self._price_sensor}'"
                    ) from e
            else:
                # Sensor unavailable and no model data
                _LOGGER.error(
                    "OptimizationCoordinator: Price sensor '%s' not available and "
                    "no historical price model data yet.",
                    self._price_sensor,
                )
                self._last_failure_reason = (
                    f"Price sensor '{self._price_sensor}' not available"
                )
                raise UpdateFailed(
                    f"Price sensor '{self._price_sensor}' not available",
                    retry_after=60,
                )

        # Get feed-in price forecast
        feed_in_sensor = self.config.get(CONF_FEED_IN_PRICE_SENSOR)
        if feed_in_sensor:
            feed_in_state = self.hass.states.get(feed_in_sensor)
            if feed_in_state and feed_in_state.state not in ("unknown", "unavailable"):
                feed_in_forecast, _ = extract_price_forecast_with_interval(
                    feed_in_state
                )
            else:
                # Sensor unavailable - fall back to fixed price
                fixed_price = float(
                    self.config.get(
                        CONF_FIXED_FEED_IN_PRICE, DEFAULT_FIXED_FEED_IN_PRICE
                    )
                )
                feed_in_forecast = [fixed_price] * len(price_forecast)
        else:
            # Use fixed feed-in price
            fixed_price = float(
                self.config.get(CONF_FIXED_FEED_IN_PRICE, DEFAULT_FIXED_FEED_IN_PRICE)
            )
            feed_in_forecast = [fixed_price] * len(price_forecast)

        # Get optimization parameters
        time_step = int(
            self.config.get(CONF_TIME_STEP_MINUTES, DEFAULT_TIME_STEP_MINUTES)
        )
        degradation_cost = float(
            self.config.get(
                CONF_DEGRADATION_COST_PER_KWH, DEFAULT_DEGRADATION_COST_PER_KWH
            )
        )
        min_spread = float(
            self.config.get(CONF_MIN_PRICE_SPREAD, DEFAULT_MIN_PRICE_SPREAD)
        )

        # Resample all forecasts to time step resolution
        resampled_prices = resample_forecast(price_forecast, price_interval, time_step)

        # Extend horizon with historical model if live forecast covers less than 24 hours
        min_horizon_steps = 24 * 60 // time_step
        if len(resampled_prices) < min_horizon_steps and self._price_model.has_data():
            steps_needed = min_horizon_steps - len(resampled_prices)
            hours_already = len(resampled_prices) * time_step / 60
            hours_for_model = (steps_needed * time_step + 59) // 60  # ceiling division
            extension_start = dt_util.now().replace(
                minute=0, second=0, microsecond=0
            ) + timedelta(hours=int(hours_already))
            weather_raw = self.weather_coordinator.data or {}
            ghi_raw = weather_raw.get("radiation_forecast", [])
            wind_raw = weather_raw.get("wind_speed_forecast", [])
            offset = int(hours_already)
            model_extension = self._price_model.forecast(
                hours=hours_for_model,
                start_time=extension_start,
                ghi_forecast=ghi_raw[offset:] if ghi_raw else None,
                wind_forecast=wind_raw[offset:] if wind_raw else None,
            )
            resampled_extension = resample_forecast(model_extension, 60, time_step)
            original_steps = len(resampled_prices)
            resampled_prices = resampled_prices + resampled_extension[:steps_needed]
            if price_forecast_source == "live":
                price_forecast_source = "live+historical_model"
            _LOGGER.debug(
                "Extended price horizon from %d to %d steps with historical model",
                original_steps,
                len(resampled_prices),
            )

        resampled_feed_in = None
        if feed_in_forecast:
            resampled_feed_in = resample_forecast(
                feed_in_forecast, price_interval, time_step
            )

        # Get PV and consumption forecasts (already hourly from forecast coordinator)
        pv_forecast = resample_forecast(
            forecast_data.get("pv_forecast_kw", []), 60, time_step
        )
        consumption_forecast = resample_forecast(
            forecast_data.get("consumption_forecast_kw", []), 60, time_step
        )

        # Horizon = length of price forecast (the binding constraint)
        n_steps = len(resampled_prices)

        # Get DC-coupled PV forecast if available
        pv_dc_forecast = None
        if forecast_data.get("pv_dc_coupled"):
            raw_dc = forecast_data.get("pv_dc_forecast_kw", [])
            if raw_dc and any(v > 0 for v in raw_dc):
                pv_dc_forecast = resample_forecast(raw_dc, 60, time_step)

        # Pad shorter forecasts to match price horizon
        if resampled_feed_in and len(resampled_feed_in) < n_steps:
            resampled_feed_in.extend(
                [resampled_feed_in[-1]] * (n_steps - len(resampled_feed_in))
            )
        while len(pv_forecast) < n_steps:
            pv_forecast.append(0.0)
        while len(consumption_forecast) < n_steps:
            consumption_forecast.append(
                consumption_forecast[-1] if consumption_forecast else 0.5
            )
        if pv_dc_forecast is not None:
            while len(pv_dc_forecast) < n_steps:
                pv_dc_forecast.append(0.0)

        # Get current battery state
        battery_state = self.get_current_battery_state()

        _LOGGER.debug("OptimizationCoordinator: Calling optimize_battery_schedule.")
        # Run optimization
        _LOGGER.debug(
            "Running optimization: SoC=%.1f%%, %d steps, %d prices",
            battery_state.soc_percent,
            n_steps,
            len(resampled_prices),
        )

        result = await self.hass.async_add_executor_job(
            optimize_battery_schedule,
            self.battery_config,
            battery_state.soc_kwh,
            resampled_prices,
            resampled_feed_in,
            pv_forecast,
            consumption_forecast,
            time_step,
            degradation_cost,
            min_spread,
            pv_dc_forecast,
        )

        self._last_result = result

        # Get current grid power: prefer real sensor, fall back to estimate
        realtime_grid_w = self._get_realtime_grid_w()
        if realtime_grid_w is not None:
            current_grid = realtime_grid_w
        else:
            # Estimate from forecast data and battery state
            current_pv_kw = forecast_data.get("current_pv_kw", 0.0)
            current_dc_pv_kw = forecast_data.get("current_dc_pv_kw", 0.0)
            current_consumption_kw = forecast_data.get("current_consumption_kw", 0.0)
            dc_pv_to_ac_kw = current_dc_pv_kw * 0.96
            total_pv_kw = current_pv_kw + dc_pv_to_ac_kw
            current_grid = (
                current_consumption_kw - total_pv_kw + battery_state.power_kw
            ) * 1000  # Convert to W

        dp_schedule_w = result.optimal_power_kw * 1000

        # Determine effective mode/power based on control mode
        if self._control_mode == MODE_ZERO_GRID:
            effective_mode = "zero_grid"
            effective_power = 0.0
        elif self._control_mode == MODE_MANUAL:
            effective_mode = "manual"
            effective_power = 0.0
        elif self._control_mode == MODE_HYBRID:
            # Hybrid: DP schedule for arbitrage, zero_grid for self-consumption
            if result.optimal_mode == "idle":
                # Optimizer wants to preserve battery capacity.
                # This means: don't charge (even with PV surplus) and don't discharge.
                #
                # Why? Two common cases:
                # 1. High feed-in price now → better to export than store
                # 2. Upcoming expensive periods → preserve capacity for discharge
                #
                # Exception: if there's consumption (grid importing), use zero_grid
                # to reduce import with available PV, without cycling the battery.
                has_upcoming_discharge = any(
                    m == "discharging" for m in result.mode_schedule[1:]
                )
                if has_upcoming_discharge and current_grid >= 0:
                    # Preserve capacity (discharge planned, no PV surplus)
                    effective_mode = "idle"
                else:
                    # Either no discharge planned, or PV surplus to capture
                    effective_mode = "zero_grid"
                effective_power = 0.0
            elif result.optimal_mode == "discharging":
                # Decide: full-rate export vs zero_grid (self-consumption only).
                # Use shadow price as the threshold: net sell value per kWh stored
                # = feed_in * sqrt(RTE). If that exceeds the shadow price (the
                # value of keeping the energy for future use), exporting is better.
                current_feed_in = (
                    resampled_feed_in[0]
                    if resampled_feed_in
                    else float(
                        self.config.get(
                            CONF_FIXED_FEED_IN_PRICE, DEFAULT_FIXED_FEED_IN_PRICE
                        )
                    )
                )
                sqrt_rte = self.battery_config.round_trip_efficiency**0.5
                if current_feed_in * sqrt_rte >= result.shadow_price_eur_kwh:
                    # Selling captures at least as much value as keeping
                    effective_mode = "discharging"
                    effective_power = result.optimal_power_kw
                else:
                    # Shadow price > sell value: energy is more valuable later
                    effective_mode = "zero_grid"
                    effective_power = 0.0
            elif result.optimal_mode == "charging" and current_grid < 0:
                current_feed_in = (
                    resampled_feed_in[0]
                    if resampled_feed_in
                    else float(
                        self.config.get(
                            CONF_FIXED_FEED_IN_PRICE, DEFAULT_FIXED_FEED_IN_PRICE
                        )
                    )
                )
                if current_feed_in < 0:
                    # Negative feed-in: exporting costs money. Use follow_schedule
                    # so curtailing PV (grid → ~0) doesn't cause a zero_grid
                    # deadlock that stops charging.
                    effective_mode = result.optimal_mode
                    effective_power = result.optimal_power_kw
                else:
                    # PV surplus available (grid exporting): use zero_grid to
                    # dynamically match the actual surplus instead of fixed-rate
                    # charging. Fixed charging may import from grid when clouds pass.
                    effective_mode = "zero_grid"
                    effective_power = 0.0
            else:
                effective_mode = result.optimal_mode
                effective_power = result.optimal_power_kw
        else:
            # follow_schedule: execute DP schedule exactly
            effective_mode = result.optimal_mode
            effective_power = result.optimal_power_kw

        # Store for real-time control loop
        self._effective_mode = effective_mode
        self._effective_power = effective_power
        self._dp_schedule_w = dp_schedule_w

        # Calculate zero-grid control action using the resolved effective mode
        controller_mode = self._resolve_controller_mode(effective_mode, current_grid)

        control_action = self.zero_grid_controller.get_control_action(
            current_grid_w=current_grid,
            current_soc_kwh=battery_state.soc_kwh,
            current_battery_w=battery_state.power_kw * 1000,
            dp_schedule_w=dp_schedule_w,
            mode=controller_mode,
        )

        # Battery-controlled zero_grid: if no power sensors but mode is zero_grid,
        # set setpoint to 0 (battery inverter will handle zero_grid with its own sensors)
        has_power_sensors = bool(
            self._power_consumption_sensors or self._power_production_sensors
        )
        if not has_power_sensors and effective_mode == "zero_grid":
            control_action["target_power_w"] = 0.0
            control_action["target_power_kw"] = 0.0
            control_action["action_mode"] = "zero_grid"

        _LOGGER.debug(
            "OptimizationCoordinator: Recording successful run at %s.",
            dt_util.utcnow(),
        )
        # Record successful run
        self._last_failure_reason = None
        self._last_success_time = dt_util.utcnow()

        return {
            "optimization_result": result,
            "battery_state": battery_state,
            "control_action": control_action,
            "control_mode": self._control_mode,
            "optimal_power_kw": effective_power,
            "optimal_mode": effective_mode,
            "schedule_power_kw": result.optimal_power_kw,
            "schedule_mode": result.optimal_mode,
            "power_schedule_kw": result.power_schedule_kw,
            "mode_schedule": result.mode_schedule,
            "soc_schedule_kwh": result.soc_schedule_kwh,
            "total_cost": result.total_cost,
            "baseline_cost": result.baseline_cost,
            "savings": round(result.savings, 2),
            "shadow_price_eur_kwh": round(result.shadow_price_eur_kwh, 4),
            "current_price": resampled_prices[0] if resampled_prices else 0.0,
            "current_feed_in_price": (
                resampled_feed_in[0]
                if resampled_feed_in
                else float(
                    self.config.get(
                        CONF_FIXED_FEED_IN_PRICE, DEFAULT_FIXED_FEED_IN_PRICE
                    )
                )
            ),
            "price_forecast_source": price_forecast_source,
            "timestamp": dt_util.utcnow(),
        }

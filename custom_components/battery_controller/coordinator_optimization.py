"""Optimization coordinator for the Battery Controller integration."""

from __future__ import annotations

import dataclasses
import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant, Event, EventStateChangedData, callback
from homeassistant.helpers.event import (
    async_track_point_in_time,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .battery_model import BatteryConfig, BatteryState, aggregate_battery_configs
from .const import (
    ACTION_CHARGING,
    ACTION_DISCHARGING,
    ACTION_IDLE,
    DC_TO_AC_INVERTER_EFFICIENCY,
    CONF_BATTERY_SOC_SENSOR,
    CONF_BATTERY_POWER_SENSOR,
    CONF_CONTROL_MODE,
    CONF_DEGRADATION_COST_PER_CYCLE,
    CONF_FEED_IN_PRICE_SENSOR,
    CONF_FIXED_FEED_IN_PRICE,
    CONF_MANUAL_POWER_SETPOINT_W,
    CONF_MIN_PRICE_SPREAD,
    CONF_POWER_CONSUMPTION_SENSORS,
    CONF_POWER_PRODUCTION_SENSORS,
    CONF_PRICE_SENSOR,
    DEFAULT_CONTROL_MODE,
    DEFAULT_DEGRADATION_COST_PER_CYCLE,
    DEFAULT_FIXED_FEED_IN_PRICE,
    DEFAULT_MANUAL_POWER_SETPOINT_W,
    DEFAULT_MIN_PRICE_SPREAD,
    CONF_ZERO_GRID_RESPONSE_TIME_S,
    DEFAULT_ZERO_GRID_RESPONSE_TIME_S,
    MODE_FOLLOW_SCHEDULE,
    MODE_HYBRID,
    MODE_MANUAL,
    MODE_ZERO_GRID,
    PRICE_CHANGE_REOPTIMIZE_THRESHOLD,
    SOC_UNCERTAINTY_RESERVE_FRACTION,
    STALE_SENSOR_MULTIPLIER,
    BATTERY_MODE_THRESHOLD_W,
    SETPOINT_STABLE_THRESHOLD_KW,
    BATTERY_POWER_CHANGE_THRESHOLD_KW,
)
from .coordinator_forecast import ForecastCoordinator
from .coordinator_weather import WeatherDataCoordinator
from .forecast_models import PriceForecastModel
from .helpers import (
    synthesize_timestamps,
    compute_step_durations_hours,
    extract_price_forecast_with_interval,
    extract_price_forecast_with_timestamps,
    get_sensor_value,
    resample_forecast,
)
from .optimizer import optimize_battery_schedule, OptimizationResult
from .zero_grid_controller import create_zero_grid_controller

_LOGGER = logging.getLogger(__name__)


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
        # Use a 60-minute fallback interval for the DataUpdateCoordinator's own
        # retry/backoff mechanism.  The primary scheduling is now event-driven:
        # price-period boundary (via _handle_price_change) + one mid-period
        # correction run.  The DC interval only fires when those triggers miss.
        super().__init__(
            hass,
            _LOGGER,
            name="Battery Controller Optimization",
            update_interval=timedelta(minutes=60),
        )

        self.weather_coordinator = weather_coordinator
        self.forecast_coordinator = forecast_coordinator
        self.config = config

        # Battery subentries: list of (subentry_id, subentry_data_dict)
        self._battery_subentries: list[tuple[str, dict[str, Any]]] = config.get(
            "battery_subentries", []
        )

        # Build per-battery configs and aggregate for optimizer
        self._individual_battery_configs: list[tuple[str, BatteryConfig]] = [
            (sid, BatteryConfig.from_subentry(d)) for sid, d in self._battery_subentries
        ]
        self.battery_config = aggregate_battery_configs(
            [cfg for _, cfg in self._individual_battery_configs]
        )

        # Per-battery state cache (updated by get_current_battery_state)
        self._per_battery_states: dict[str, BatteryState] = {}

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

        # Real-time sensors for zero_grid control (grid power only; battery sensors in subentries)
        self._power_consumption_sensors = config.get(CONF_POWER_CONSUMPTION_SENSORS, [])
        self._power_production_sensors = config.get(CONF_POWER_PRODUCTION_SENSORS, [])
        self._unsub_realtime: Any | None = None

        # First SoC sensor from any battery subentry (used for availability tracking)
        self._battery_soc_sensor: str | None = (
            self._battery_subentries[0][1].get(CONF_BATTERY_SOC_SENSOR)
            if self._battery_subentries
            else None
        )

        # Last optimization result and effective mode (persists between real-time updates)
        self._last_result: OptimizationResult | None = None
        self._last_shadow_price: float | None = None
        self._effective_mode: str = ACTION_IDLE
        self._effective_power: float = 0.0
        # Schedule power currently sent to the controller after all mode resolution
        # and commitment filtering. This is not necessarily the raw optimizer output.
        self._controller_schedule_w: float = 0.0

        # Commitment filter: prevent switching active charge/discharge to idle unless
        # the price has moved enough to make the change economically justified.
        self._committed_action: str = ACTION_IDLE
        self._committed_price: float = 0.0
        self._committed_power: float = 0.0
        self._committed_step_start: datetime | None = None

        # Failure tracking and cascade listeners
        self._last_failure_reason: str | None = None
        self._last_success_time: datetime | None = None
        self._unsub_soc: Any | None = None
        self._unsub_forecast: Any | None = None
        self._unsub_mid_period_timer: Any | None = None
        self._unsub_price_model_refresh: Any | None = None
        # Last seen price period start; used to detect period boundary transitions.
        self._last_period_start: datetime | None = None

        # Historical price forecast model (fallback when day-ahead not yet published)
        self._price_model = PriceForecastModel(
            hass=hass,
            price_sensor_id=config.get(CONF_PRICE_SENSOR, ""),
            entry_id=config.get("entry_id"),
            history_days=28,
        )
        # Separate historical model for feed-in price (learns its own pattern)
        self._feed_in_price_model = PriceForecastModel(
            hass=hass,
            price_sensor_id=config.get(CONF_FEED_IN_PRICE_SENSOR, ""),
            entry_id=config.get("entry_id"),
            history_days=28,
        )

        # Enabled flag: when False _async_update_data returns cached data immediately
        # without re-running the optimizer. The scheduler keeps running so it
        # is trivial to re-enable without manual intervention.
        self._optimization_enabled: bool = True

        # Guard against concurrent optimizer runs (e.g. price change + timer overlap).
        self._optimization_running: bool = False

        # When a trigger arrives while an optimization is already running, queue one
        # re-run rather than dropping the request entirely (P3.2).
        self._pending_optimization: bool = False

        # Last hybrid mode decision for hysteresis (P3.1).
        # Tracks whether we were in "discharging" (schedule) or "zero_grid" state
        # so small oscillations around the shadow-price threshold are damped.
        self._last_hybrid_decision: str = "zero_grid"

    @property
    def control_mode(self) -> str:
        """Get current control mode."""
        return self._control_mode

    @control_mode.setter
    def control_mode(self, mode: str) -> None:
        """Set control mode and reset commitment state to prevent stale locks."""
        self._control_mode = mode
        self._committed_action = ACTION_IDLE
        self._committed_price = 0.0
        self._committed_power = 0.0
        self._committed_step_start = None

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

    async def _handle_price_model_refresh(self, now: datetime) -> None:
        """Refresh historical price model from HA recorder (daily timer)."""
        _LOGGER.debug("Daily price model refresh triggered at %s", now)
        await self._price_model.async_update_pattern()
        await self._feed_in_price_model.async_update_pattern()

    async def async_setup(self) -> None:
        """Set up event tracking for price changes and real-time control."""
        await self._price_model.async_update_pattern()
        await self._feed_in_price_model.async_update_pattern()

        # Re-learn price pattern every 24 h so new data is picked up automatically,
        # and so the model becomes available shortly after a fresh install.
        self._unsub_price_model_refresh = async_track_time_interval(
            self.hass,
            self._handle_price_model_refresh,
            timedelta(hours=24),
        )

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

    @callback
    def _handle_price_change(self, event: Event[EventStateChangedData]) -> None:
        """Handle price sensor state changes.

        Triggers optimization at each price period boundary and schedules a
        single mid-period correction run for SoC drift.  For sensors without
        timestamp attributes, falls back to a >10% threshold check.
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

        # Try to read the current period start from sensor timestamps.
        try:
            _, start_times, interval_minutes = extract_price_forecast_with_timestamps(
                new_state
            )
            period_start: datetime | None = start_times[0] if start_times else None
        except Exception:  # noqa: BLE001
            period_start = None
            interval_minutes = 60

        if was_unavailable:
            _LOGGER.debug(
                "Price sensor '%s' became available (%.4f), triggering optimization",
                self._price_sensor,
                new_price,
            )
            self._last_price = new_price
            self._last_period_start = period_start
            self._schedule_mid_period_run(period_start, interval_minutes)
            self.hass.async_create_task(self.async_request_refresh())
        elif period_start is not None and period_start != self._last_period_start:
            # New price period — primary optimization trigger.
            _LOGGER.debug(
                "New price period started at %s, triggering optimization", period_start
            )
            self._last_price = new_price
            self._last_period_start = period_start
            self._schedule_mid_period_run(period_start, interval_minutes)
            self.hass.async_create_task(self.async_request_refresh())
        elif self._last_price is not None and self._last_price != 0:
            # Same period or no timestamp info — fallback threshold check.
            change_pct = abs(new_price - self._last_price) / abs(self._last_price)
            if change_pct >= PRICE_CHANGE_REOPTIMIZE_THRESHOLD:
                _LOGGER.debug(
                    "Significant price change: %.2f%%, triggering optimization",
                    change_pct * 100,
                )
                self.hass.async_create_task(self.async_request_refresh())
            self._last_price = new_price

    @callback
    def _schedule_mid_period_run(
        self, period_start: datetime | None, interval_minutes: int
    ) -> None:
        """Schedule a single mid-period correction run at period_start + interval/2.

        Cancels any previously scheduled mid-period timer.  Skipped when the
        mid-point is already in the past (e.g. HA restart late in a period).
        """
        if self._unsub_mid_period_timer:
            self._unsub_mid_period_timer()
            self._unsub_mid_period_timer = None

        if period_start is None:
            return

        mid_point = period_start + timedelta(minutes=interval_minutes / 2)
        now = dt_util.utcnow()
        if mid_point <= now:
            _LOGGER.debug(
                "Mid-period correction point %s is already past, skipping", mid_point
            )
            return

        @callback
        def _fire(_now: datetime) -> None:
            self._unsub_mid_period_timer = None
            _LOGGER.debug("Mid-period correction run triggered at %s", _now)
            self.hass.async_create_task(self.async_request_refresh())

        self._unsub_mid_period_timer = async_track_point_in_time(
            self.hass, _fire, mid_point
        )
        _LOGGER.debug(
            "Mid-period correction scheduled for %s (%d min from now)",
            mid_point,
            int((mid_point - now).total_seconds() / 60),
        )

    @callback
    def _handle_soc_available(self, event: Event[EventStateChangedData]) -> None:
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
            self.hass.async_create_task(self.async_request_refresh())

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
            _LOGGER.debug(
                "Grid power sensors unavailable; real-time zero_grid update skipped"
            )
            return

        # Stale sensor detection (P4.1): if the first grid sensor has not updated
        # recently, treat its reading as unreliable and skip the zero_grid correction.
        stale_limit_s = STALE_SENSOR_MULTIPLIER * float(
            self.config.get(
                CONF_ZERO_GRID_RESPONSE_TIME_S, DEFAULT_ZERO_GRID_RESPONSE_TIME_S
            )
        )
        if self._power_consumption_sensors:
            first_sensor = self._power_consumption_sensors[0]
            state = self.hass.states.get(first_sensor)
            if state is not None and state.last_updated is not None:
                age_s = (dt_util.utcnow() - state.last_updated).total_seconds()
                if age_s > stale_limit_s:
                    _LOGGER.debug(
                        "Grid power sensor '%s' is stale (%.0f s old, limit %.0f s); "
                        "skipping zero_grid correction",
                        first_sensor,
                        age_s,
                        stale_limit_s,
                    )
                    return

        # Read current battery state
        battery_state = self.get_current_battery_state()

        # Re-derive effective mode from the current control mode so that a
        # mode switch (e.g. hybrid → follow_schedule) takes effect in the
        # real-time loop immediately, without waiting for the next 15-min run.
        if self._control_mode == MODE_ZERO_GRID:
            rt_effective_mode = "zero_grid"
            controller_schedule_w = 0.0
        elif self._control_mode == MODE_MANUAL:
            # Manual reads the live setpoint (may change between 15-min runs)
            rt_effective_mode = "manual"
            manual_w = self._get_manual_setpoint_w()
            self._controller_schedule_w = manual_w
            controller_schedule_w = manual_w
        else:
            # follow_schedule / hybrid: use cached values from last optimisation
            # run (includes commitment filter). This avoids bypassing the filter
            # and publishing jittery setpoints.
            rt_effective_mode = self._effective_mode
            controller_schedule_w = self._controller_schedule_w

        controller_mode = self._resolve_controller_mode(
            rt_effective_mode, current_grid_w
        )

        # Recalculate zero_grid setpoint with actual sensor data
        control_action = self.zero_grid_controller.get_control_action(
            current_grid_w=current_grid_w,
            current_soc_kwh=battery_state.soc_kwh,
            current_battery_w=battery_state.power_kw * 1000,
            dp_schedule_w=controller_schedule_w,
            mode=controller_mode,
        )

        prev_target = (
            self.data.get("control_action", {}).get("target_power_kw")
            if self.data
            else None
        )
        new_target = control_action["target_power_kw"]
        setpoint_stable = (
            prev_target is not None
            and abs(new_target - prev_target) < SETPOINT_STABLE_THRESHOLD_KW
        )

        if setpoint_stable:
            # Setpoint unchanged — still update battery state if power changed,
            # so that BatteryPowerSensor stays current even when setpoint is stable.
            old_state = self.data.get("battery_state") if self.data else None
            if (
                old_state is None
                or abs(battery_state.power_kw - old_state.power_kw)
                > BATTERY_POWER_CHANGE_THRESHOLD_KW
            ):
                self.async_set_updated_data(
                    {
                        **self.data,
                        "battery_state": battery_state,
                        "per_battery_states": dict(self._per_battery_states),
                    }
                )
            return

        # Setpoint changed — full update including control action and setpoints
        battery_setpoints = self._split_setpoint(control_action["target_power_kw"])
        self.async_set_updated_data(
            {
                **self.data,
                "control_action": control_action,
                "battery_state": battery_state,
                "per_battery_states": dict(self._per_battery_states),
                "battery_setpoints": battery_setpoints,
                "optimal_power_kw": control_action["target_power_kw"],
                "optimal_mode": control_action["action_mode"],
            }
        )

    def _get_manual_setpoint_w(self) -> float:
        """Read the live manual power setpoint from entry options.

        Reads from the live config entry so changes made via the number entity
        take effect immediately without waiting for the next optimizer run.

        The number entity uses the sensor convention (positive = discharge, negative =
        charge). We negate here to match the internal controller convention
        (positive = charge, negative = discharge).
        """
        entry_id = self.config.get("entry_id", "")
        entry = self.hass.config_entries.async_get_known_entry(entry_id)
        if entry is None:
            return DEFAULT_MANUAL_POWER_SETPOINT_W
        stored = float(
            entry.options.get(
                CONF_MANUAL_POWER_SETPOINT_W, DEFAULT_MANUAL_POWER_SETPOINT_W
            )
        )
        # Negate: user enters positive=discharge, controller expects positive=charge
        return -stored

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
        # Upgrade idle → zero_grid only in zero_grid/hybrid modes (not follow_schedule
        # or manual, where idle must mean truly stop). Only when grid is actually
        # exporting (negative), i.e. real PV surplus — not just near-zero import noise.
        if (
            effective_mode == ACTION_IDLE
            and self._control_mode not in (MODE_FOLLOW_SCHEDULE, MODE_MANUAL)
            and current_grid_w < 0
            and has_power_sensors
        ):
            return "zero_grid"
        if effective_mode == ACTION_IDLE:
            return ACTION_IDLE
        if effective_mode == "manual":
            return "manual"
        if effective_mode in (ACTION_CHARGING, ACTION_DISCHARGING):
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
        if self._unsub_mid_period_timer:
            self._unsub_mid_period_timer()
            self._unsub_mid_period_timer = None
        if self._unsub_price_model_refresh:
            self._unsub_price_model_refresh()
            self._unsub_price_model_refresh = None
        if self._unsub_realtime:
            self._unsub_realtime()
            self._unsub_realtime = None

    def _read_battery_state(
        self,
        subentry_data: dict[str, Any],
        battery_config: BatteryConfig,
        fallback_soc_percent: float = 50.0,
    ) -> BatteryState:
        """Read state for one battery subentry."""
        soc_sensor = subentry_data.get(CONF_BATTERY_SOC_SENSOR)
        power_sensor = subentry_data.get(CONF_BATTERY_POWER_SENSOR)

        soc_value = get_sensor_value(self.hass, soc_sensor, fallback_soc_percent)
        power_value = get_sensor_value(self.hass, power_sensor, 0.0)

        if soc_sensor:
            state = self.hass.states.get(soc_sensor)
            if state and state.state not in ("unknown", "unavailable"):
                unit = state.attributes.get("unit_of_measurement", "")
                if unit == "kWh":
                    soc_kwh = soc_value
                    soc_percent = (soc_kwh / battery_config.capacity_kwh) * 100
                else:
                    soc_percent = soc_value
                    soc_kwh = (soc_percent / 100) * battery_config.capacity_kwh
            else:
                soc_percent = fallback_soc_percent
                soc_kwh = (soc_percent / 100) * battery_config.capacity_kwh
        else:
            soc_percent = soc_value
            soc_kwh = (soc_percent / 100) * battery_config.capacity_kwh

        power_kw = power_value  # Assume kW unless sensor says otherwise
        if power_sensor:
            state = self.hass.states.get(power_sensor)
            if state:
                unit = state.attributes.get("unit_of_measurement", "W")
                if unit == "W":
                    power_kw = power_value / 1000  # Convert W → kW
                elif unit == "kW":
                    power_kw = power_value  # Already kW
                else:
                    _LOGGER.warning(
                        "Battery power sensor '%s' has unexpected unit '%s'; "
                        "treating value as kW",
                        power_sensor,
                        unit,
                    )

        power_w = power_kw * 1000
        if power_w > BATTERY_MODE_THRESHOLD_W:
            mode = ACTION_CHARGING
        elif power_w < -BATTERY_MODE_THRESHOLD_W:
            mode = ACTION_DISCHARGING
        else:
            mode = ACTION_IDLE

        return BatteryState(
            soc_kwh=soc_kwh, soc_percent=soc_percent, power_kw=power_kw, mode=mode
        )

    def get_current_battery_state(self) -> BatteryState:
        """Get combined battery state from all battery subentries.

        Also caches per-battery states in self._per_battery_states for setpoint splitting.
        """
        if not self._individual_battery_configs:
            # No battery subentries — return safe default
            return BatteryState(
                soc_kwh=0.0, soc_percent=50.0, power_kw=0.0, mode="idle"
            )

        per_battery: dict[str, BatteryState] = {}
        total_soc_kwh = 0.0
        total_power_kw = 0.0
        total_capacity_kwh = 0.0

        for (sid, battery_config), (_, subentry_data) in zip(
            self._individual_battery_configs, self._battery_subentries
        ):
            # Use cached state as fallback SoC
            cached = self._per_battery_states.get(sid)
            fallback = cached.soc_percent if cached else 50.0

            state = self._read_battery_state(subentry_data, battery_config, fallback)
            per_battery[sid] = state
            total_soc_kwh += state.soc_kwh
            total_power_kw += state.power_kw
            total_capacity_kwh += battery_config.capacity_kwh

        self._per_battery_states = per_battery

        combined_soc_percent = (
            (total_soc_kwh / total_capacity_kwh) * 100.0
            if total_capacity_kwh > 0
            else 50.0
        )
        power_w = total_power_kw * 1000
        if power_w > BATTERY_MODE_THRESHOLD_W:
            mode = ACTION_CHARGING
        elif power_w < -BATTERY_MODE_THRESHOLD_W:
            mode = ACTION_DISCHARGING
        else:
            mode = ACTION_IDLE

        return BatteryState(
            soc_kwh=total_soc_kwh,
            soc_percent=combined_soc_percent,
            power_kw=total_power_kw,
            mode=mode,
        )

    def _split_setpoint(self, total_kw: float) -> dict[str, float]:
        """Split combined setpoint (kW, positive=charge) to per-battery setpoints.

        Distributes proportionally to available headroom (charging) or available
        energy (discharging).  Each battery is clamped to its individual power limit.
        """
        if not self._individual_battery_configs:
            return {}

        result: dict[str, float] = {}

        if total_kw > 0:  # charging
            headrooms = {
                sid: max(0.0, cfg.max_soc_kwh - self._per_battery_states[sid].soc_kwh)
                if sid in self._per_battery_states
                else cfg.max_soc_kwh * 0.5
                for sid, cfg in self._individual_battery_configs
            }
            total_headroom = sum(headrooms.values())
            for sid, cfg in self._individual_battery_configs:
                if total_headroom > 0:
                    raw = total_kw * headrooms[sid] / total_headroom
                else:
                    raw = 0.0
                result[sid] = min(raw, cfg.max_charge_power_kw)

        elif total_kw < 0:  # discharging
            availables = {
                sid: max(0.0, self._per_battery_states[sid].soc_kwh - cfg.min_soc_kwh)
                if sid in self._per_battery_states
                else cfg.capacity_kwh * 0.4
                for sid, cfg in self._individual_battery_configs
            }
            total_available = sum(availables.values())
            for sid, cfg in self._individual_battery_configs:
                if total_available > 0:
                    raw = total_kw * availables[sid] / total_available
                else:
                    raw = 0.0
                result[sid] = max(raw, -cfg.max_discharge_power_kw)

        else:
            result = {sid: 0.0 for sid, _ in self._individual_battery_configs}

        return result

    def _refresh_battery_config(self) -> None:
        """Re-read BatteryConfigs from live battery subentry data.

        Called at the start of each optimization run so that SoC limit changes
        made in the subentry config flow take effect without a full reload.
        """
        entry_id = self.config.get("entry_id", "")
        entry = self.hass.config_entries.async_get_known_entry(entry_id)
        if entry is None:
            return

        self._individual_battery_configs = [
            (sid, BatteryConfig.from_subentry(dict(s.data)))
            for sid, s in (
                (sid, entry.subentries[sid])
                for sid, _ in self._battery_subentries
                if sid in entry.subentries
            )
        ]
        self.battery_config = aggregate_battery_configs(
            [cfg for _, cfg in self._individual_battery_configs]
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Run battery optimization."""
        _LOGGER.debug("OptimizationCoordinator: _async_update_data started.")

        # Guard against concurrent optimizer runs (timer + price-change overlap).
        # Queue one pending re-run so triggers that arrive mid-run are not lost (P3.2).
        if self._optimization_running:
            _LOGGER.debug(
                "OptimizationCoordinator: previous run still in progress, queuing."
            )
            self._pending_optimization = True
            if self.data is not None:
                return self.data
            raise UpdateFailed("Optimization already in progress")

        # When disabled via switch, skip re-running the optimizer but keep the
        # 15-minute scheduler alive so re-enabling resumes without any manual nudge.
        if not self._optimization_enabled:
            _LOGGER.debug(
                "OptimizationCoordinator: Optimization disabled, returning cached data."
            )
            if self.data is not None:
                return self.data

        self._optimization_running = True
        self._pending_optimization = False
        try:
            return await self._run_optimization()
        finally:
            self._optimization_running = False
            if self._pending_optimization:
                self._pending_optimization = False
                _LOGGER.debug(
                    "OptimizationCoordinator: pending trigger detected, scheduling re-run."
                )
                self.hass.async_create_task(self.async_request_refresh())

    async def _run_optimization(self) -> dict[str, Any]:
        """Inner optimization logic (called only when not already running)."""
        # Re-read SoC limits and other hardware parameters from live options
        self._refresh_battery_config()

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
        price_start_times: list = []
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

        if sensor_ok and price_state is not None:
            price_forecast, price_start_times, price_interval = (
                extract_price_forecast_with_timestamps(price_state)
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
            elif sensor_ok and price_state is not None:
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
        feed_in_is_dynamic = False  # True when feed-in came from a live sensor forecast
        feed_in_sensor = self.config.get(CONF_FEED_IN_PRICE_SENSOR)
        if feed_in_sensor:
            feed_in_state = self.hass.states.get(feed_in_sensor)
            if feed_in_state and feed_in_state.state not in ("unknown", "unavailable"):
                feed_in_forecast, _ = extract_price_forecast_with_interval(
                    feed_in_state
                )
                feed_in_is_dynamic = True
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

        # Get optimization parameters — read runtime-tunable values from live options
        entry_id = self.config.get("entry_id", "")
        live_entry = self.hass.config_entries.async_get_known_entry(entry_id)
        live_options = live_entry.options if live_entry is not None else {}
        degradation_cost = float(
            live_options.get(
                CONF_DEGRADATION_COST_PER_CYCLE,
                self.config.get(
                    CONF_DEGRADATION_COST_PER_CYCLE, DEFAULT_DEGRADATION_COST_PER_CYCLE
                ),
            )
        )
        min_spread = float(
            live_options.get(
                CONF_MIN_PRICE_SPREAD,
                self.config.get(CONF_MIN_PRICE_SPREAD, DEFAULT_MIN_PRICE_SPREAD),
            )
        )

        # Synthesise start_times for fallback paths that have no real timestamps
        now_utc = dt_util.utcnow()
        if not price_start_times and price_forecast:
            price_start_times = synthesize_timestamps(
                now_utc, price_interval, len(price_forecast)
            )

        # price_forecast is already at native price_interval resolution — no resample needed.
        resampled_prices = price_forecast

        # Extend horizon with historical model if live forecast covers less than 36 hours
        min_horizon_steps = 36 * 60 // price_interval
        if len(resampled_prices) < min_horizon_steps and self._price_model.has_data():
            steps_needed = min_horizon_steps - len(resampled_prices)
            hours_already = len(resampled_prices) * price_interval / 60
            hours_for_model = (steps_needed * price_interval + 59) // 60  # ceiling
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
            resampled_extension = resample_forecast(model_extension, 60, price_interval)
            original_steps = len(resampled_prices)
            resampled_prices = resampled_prices + resampled_extension[:steps_needed]
            # Synthesise timestamps for the extension steps
            last_ts = price_start_times[-1] if price_start_times else now_utc
            for i in range(1, len(resampled_prices) - original_steps + 1):
                price_start_times.append(
                    last_ts + timedelta(minutes=i * price_interval)
                )
            if price_forecast_source == "live":
                price_forecast_source = "live+historical_model"
            _LOGGER.debug(
                "Extended price horizon from %d to %d steps with historical model",
                original_steps,
                len(resampled_prices),
            )

        # Generate model forecast for price accuracy comparison.
        # Always computed when live prices are used, so users can compare prediction vs actual.
        price_forecast_model: list[float] | None = None
        if self._price_model.has_data() and price_forecast_source.startswith("live"):
            _weather = self.weather_coordinator.data or {}
            total_hours = (len(resampled_prices) * price_interval + 59) // 60
            _model_raw = self._price_model.forecast(
                hours=total_hours,
                ghi_forecast=_weather.get("radiation_forecast"),
                wind_forecast=_weather.get("wind_speed_forecast"),
            )
            _model_resampled = resample_forecast(_model_raw, 60, price_interval)
            price_forecast_model = _model_resampled[: len(resampled_prices)]

        # Compute per-step durations: first step = remaining time in current price period,
        # subsequent steps = full price_interval each.
        step_durations_hours = compute_step_durations_hours(
            price_start_times, price_interval, now_utc
        )
        # Ensure step_durations_hours matches the number of price steps
        if len(step_durations_hours) < len(resampled_prices):
            step_durations_hours += [price_interval / 60.0] * (
                len(resampled_prices) - len(step_durations_hours)
            )

        # Compute absolute UTC start time for each schedule step so the chart
        # can render correct timestamps regardless of when the sensor is read.
        # Step 0 starts at the current optimizer run time (now_utc); subsequent
        # steps start at the price-period boundaries from the sensor.
        step_start_times_iso: list[str] = [now_utc.isoformat()]
        for ts in price_start_times[1 : len(resampled_prices)]:
            step_start_times_iso.append(ts.isoformat())

        resampled_feed_in = None
        if feed_in_forecast:
            resampled_feed_in = resample_forecast(
                feed_in_forecast, price_interval, price_interval
            )

        # Resample hourly PV / consumption forecasts to the price sensor's native interval
        pv_forecast = resample_forecast(
            forecast_data.get("pv_forecast_kw", []), 60, price_interval
        )
        consumption_forecast = resample_forecast(
            forecast_data.get("consumption_forecast_kw", []), 60, price_interval
        )

        # Horizon = length of price forecast (the binding constraint)
        n_steps = len(resampled_prices)

        # Get DC-coupled PV forecast if available
        pv_dc_forecast = None
        if forecast_data.get("pv_dc_coupled"):
            raw_dc = forecast_data.get("pv_dc_forecast_kw", [])
            if raw_dc and any(v > 0 for v in raw_dc):
                pv_dc_forecast = resample_forecast(raw_dc, 60, price_interval)

        # Pad shorter forecasts to match price horizon.
        # Priority: own feed-in model → grid model × ratio → last value.
        if resampled_feed_in and len(resampled_feed_in) < n_steps:
            steps_needed = n_steps - len(resampled_feed_in)
            hours_already = len(resampled_feed_in) * price_interval / 60
            hours_for_model = (steps_needed * price_interval + 59) // 60
            extension_start = dt_util.now().replace(
                minute=0, second=0, microsecond=0
            ) + timedelta(hours=int(hours_already))
            weather_raw = self.weather_coordinator.data or {}
            ghi_raw = weather_raw.get("radiation_forecast", [])
            wind_raw = weather_raw.get("wind_speed_forecast", [])
            offset = int(hours_already)

            if feed_in_is_dynamic and self._feed_in_price_model.has_data():
                # Own feed-in model has historical data — use it directly.
                model_ext = self._feed_in_price_model.forecast(
                    hours=hours_for_model,
                    start_time=extension_start,
                    ghi_forecast=ghi_raw[offset:] if ghi_raw else None,
                    wind_forecast=wind_raw[offset:] if wind_raw else None,
                )
                resampled_ext = resample_forecast(model_ext, 60, price_interval)
                resampled_feed_in.extend(resampled_ext[:steps_needed])
                _LOGGER.debug(
                    "Extended feed-in horizon by %d steps using own feed-in model",
                    steps_needed,
                )
            else:
                resampled_feed_in.extend([resampled_feed_in[-1]] * steps_needed)
        while len(pv_forecast) < n_steps:
            pv_forecast.append(0.0)
        while len(consumption_forecast) < n_steps:
            consumption_forecast.append(
                consumption_forecast[-1] if consumption_forecast else 0.5
            )
        if pv_dc_forecast is not None:
            while len(pv_dc_forecast) < n_steps:
                pv_dc_forecast.append(0.0)

        # Derive feed-in predicted forecast for accuracy comparison.
        # Priority: own feed-in model → grid model × ratio.
        feed_in_price_forecast_model: list[float] | None = None
        if (
            feed_in_is_dynamic
            and price_forecast_source.startswith("live")
            and self._feed_in_price_model.has_data()
        ):
            _weather = self.weather_coordinator.data or {}
            total_hours = (len(resampled_prices) * price_interval + 59) // 60
            _fi_raw = self._feed_in_price_model.forecast(
                hours=total_hours,
                ghi_forecast=_weather.get("radiation_forecast"),
                wind_forecast=_weather.get("wind_speed_forecast"),
            )
            _fi_resampled = resample_forecast(_fi_raw, 60, price_interval)
            feed_in_price_forecast_model = _fi_resampled[: len(resampled_prices)]

        # Get current battery state
        battery_state = self.get_current_battery_state()

        # Uncertainty-based SoC reserve (P2.3): when GHI forecast is highly variable
        # (cloudy/intermittent conditions), keep extra buffer so the battery is not
        # discharged based on optimistic solar estimates that don't materialise.
        battery_config = self.battery_config
        weather_raw = self.weather_coordinator.data or {}
        radiation_forecast_raw = weather_raw.get("radiation_forecast", [])
        daylight_ghi = [v for v in radiation_forecast_raw[:24] if v > 50.0]
        if len(daylight_ghi) > 2:
            avg_ghi = sum(daylight_ghi) / len(daylight_ghi)
            variance_ghi = sum((v - avg_ghi) ** 2 for v in daylight_ghi) / len(
                daylight_ghi
            )
            cv_ghi = (variance_ghi**0.5 / avg_ghi) if avg_ghi > 0 else 0.0
            # Scale reserve linearly with coefficient of variation, up to SOC_UNCERTAINTY_RESERVE_FRACTION of capacity
            uncertainty_reserve_kwh = min(
                SOC_UNCERTAINTY_RESERVE_FRACTION * battery_config.capacity_kwh,
                cv_ghi * SOC_UNCERTAINTY_RESERVE_FRACTION * battery_config.capacity_kwh,
            )
            if uncertainty_reserve_kwh > 0.01:
                extra_pct = (
                    uncertainty_reserve_kwh / battery_config.capacity_kwh
                ) * 100.0
                new_min_pct = min(
                    battery_config.min_soc_percent + extra_pct,
                    battery_config.max_soc_percent - 5.0,
                )
                battery_config = dataclasses.replace(
                    battery_config, min_soc_percent=new_min_pct
                )
                _LOGGER.debug(
                    "Forecast uncertainty (CV=%.2f) → min_soc raised by %.1f%% to %.1f%%",
                    cv_ghi,
                    extra_pct,
                    new_min_pct,
                )

        _LOGGER.debug("OptimizationCoordinator: Calling optimize_battery_schedule.")
        # Run optimization
        _LOGGER.debug(
            "Running optimization: SoC=%.1f%%, %d steps, %d prices",
            battery_state.soc_percent,
            n_steps,
            len(resampled_prices),
        )

        _LOGGER.debug(
            "Terminal shadow price: %s",
            f"{self._last_shadow_price:.4f} €/kWh"
            if self._last_shadow_price is not None
            else "none (first run, using feed-in tail)",
        )
        # Convert degradation cost from per-cycle to per-kWh throughput for the optimizer.
        # One cycle = max_soc_kwh - min_soc_kwh (usable capacity).
        usable_kwh = battery_config.max_soc_kwh - battery_config.min_soc_kwh
        degradation_cost_per_kwh = (
            degradation_cost / usable_kwh if usable_kwh > 0 else degradation_cost
        )

        result = await self.hass.async_add_executor_job(
            optimize_battery_schedule,
            battery_config,
            battery_state.soc_kwh,
            resampled_prices,
            resampled_feed_in,
            pv_forecast,
            consumption_forecast,
            step_durations_hours,
            degradation_cost_per_kwh,
            min_spread,
            pv_dc_forecast,
            self._last_shadow_price,
        )

        self._last_shadow_price = result.shadow_price_eur_kwh
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
            dc_pv_to_ac_kw = current_dc_pv_kw * DC_TO_AC_INVERTER_EFFICIENCY
            total_pv_kw = current_pv_kw + dc_pv_to_ac_kw
            current_grid = (
                current_consumption_kw - total_pv_kw + battery_state.power_kw
            ) * 1000  # Convert to W

        # Use the price period start (datetime) for commitment comparison, not the
        # ISO string. This avoids fragile string equality for timestamp comparison.
        current_step_start: datetime | None = (
            price_start_times[0] if price_start_times else None
        )

        # Determine effective mode/power based on control mode
        if self._control_mode == MODE_ZERO_GRID:
            effective_mode = "zero_grid"
            effective_power = 0.0
        elif self._control_mode == MODE_MANUAL:
            manual_w = self._get_manual_setpoint_w()
            effective_mode = "manual"
            effective_power = manual_w / 1000  # kW for output sensors
        elif self._control_mode == MODE_HYBRID:
            # Hybrid: DP schedule for arbitrage, zero_grid for self-consumption
            if result.optimal_mode == ACTION_IDLE:
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
                    m == ACTION_DISCHARGING for m in result.mode_schedule[1:]
                )
                if has_upcoming_discharge and current_grid >= 0:
                    # Preserve capacity (discharge planned, no PV surplus)
                    effective_mode = ACTION_IDLE
                else:
                    # Either no discharge planned, or PV surplus to capture
                    effective_mode = "zero_grid"
                effective_power = 0.0
            elif result.optimal_mode == ACTION_DISCHARGING:
                # Decide: full-rate export vs zero_grid (self-consumption only).
                # Use shadow price as the threshold: net sell value per kWh stored
                # = feed_in * sqrt(RTE). If that exceeds the shadow price (the
                # value of keeping the energy for future use), exporting is better.
                #
                # Hysteresis (P3.1): apply a ±5% band around the threshold to prevent
                # oscillation when shadow_price ≈ net_sell_value.
                # • Was discharging → continue unless net_sell_value < threshold × 0.95
                # • Was not discharging → start only if net_sell_value ≥ threshold × 1.05
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
                net_sell_value = current_feed_in * sqrt_rte
                threshold = result.shadow_price_eur_kwh
                if self._last_hybrid_decision == ACTION_DISCHARGING:
                    should_discharge = net_sell_value >= threshold * 0.95
                else:
                    should_discharge = net_sell_value >= threshold * 1.05

                if should_discharge:
                    effective_mode = ACTION_DISCHARGING
                    effective_power = result.optimal_power_kw
                    self._last_hybrid_decision = ACTION_DISCHARGING
                else:
                    # Shadow price > sell value: energy is more valuable later
                    effective_mode = "zero_grid"
                    effective_power = 0.0
                    self._last_hybrid_decision = "zero_grid"
            elif result.optimal_mode == ACTION_CHARGING and current_grid < 0:
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

        # Commitment filter: don't switch an active charge/discharge to idle unless
        # the price has moved beyond the oscillation-filter threshold (same economics
        # as the post-DP oscillation filter in optimizer.py).
        if self._control_mode not in (MODE_ZERO_GRID, MODE_MANUAL):
            sqrt_rte = battery_config.round_trip_efficiency**0.5
            commit_spread = degradation_cost_per_kwh * 2.0 / sqrt_rte + min_spread
            current_price = resampled_prices[0] if resampled_prices else 0.0
            soc_at_limit = (
                battery_state.soc_kwh <= battery_config.min_soc_kwh * 1.02
                or battery_state.soc_kwh >= battery_config.max_soc_kwh * 0.98
            )
            same_price_period = (
                current_step_start is not None
                and current_step_start == self._committed_step_start
            )
            direction_flip = (
                self._committed_action == ACTION_CHARGING
                and effective_mode == ACTION_DISCHARGING
            ) or (
                self._committed_action == ACTION_DISCHARGING
                and effective_mode == ACTION_CHARGING
            )
            if self._committed_action == ACTION_CHARGING:
                price_jumped = current_price > self._committed_price + commit_spread
            elif self._committed_action == ACTION_DISCHARGING:
                price_jumped = current_price < self._committed_price - commit_spread
            else:
                price_jumped = False

            if (
                self._committed_action in (ACTION_CHARGING, ACTION_DISCHARGING)
                and same_price_period
                and not price_jumped
                and not soc_at_limit
                and not direction_flip
            ):
                if effective_mode == self._committed_action:
                    # Same direction within the same price period: lock power so
                    # the 15-min re-optimizations don't produce erratic setpoints.
                    _LOGGER.debug(
                        "Commitment filter: locking %s power at %.0fW "
                        "(price Δ=%.3f < commit_spread=%.3f)",
                        self._committed_action,
                        self._committed_power * 1000,
                        abs(current_price - self._committed_price),
                        commit_spread,
                    )
                    effective_power = self._committed_power
                elif effective_mode == ACTION_IDLE:
                    # Prevent switching an active charge/discharge to idle.
                    _LOGGER.debug(
                        "Commitment filter: keeping %s (price Δ=%.3f < commit_spread=%.3f, "
                        "soc_at_limit=%s)",
                        self._committed_action,
                        abs(current_price - self._committed_price),
                        commit_spread,
                        soc_at_limit,
                    )
                    effective_mode = self._committed_action
                    effective_power = self._committed_power
                else:
                    # Direction flip bypassed the guard — update commitment.
                    self._committed_action = effective_mode
                    self._committed_price = current_price
                    self._committed_power = effective_power
                    self._committed_step_start = current_step_start
            else:
                self._committed_action = effective_mode
                self._committed_price = current_price
                self._committed_power = effective_power
                self._committed_step_start = current_step_start

        controller_schedule_w = (
            effective_power * 1000 if effective_mode != ACTION_IDLE else 0.0
        )

        # Store for real-time control loop
        self._effective_mode = effective_mode
        self._effective_power = effective_power
        self._controller_schedule_w = controller_schedule_w

        # Calculate zero-grid control action using the resolved effective mode
        controller_mode = self._resolve_controller_mode(effective_mode, current_grid)

        control_action = self.zero_grid_controller.get_control_action(
            current_grid_w=current_grid,
            current_soc_kwh=battery_state.soc_kwh,
            current_battery_w=battery_state.power_kw * 1000,
            dp_schedule_w=controller_schedule_w,
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

        # Split combined setpoint across individual batteries
        combined_setpoint_kw = control_action["target_power_kw"]  # positive=charge
        battery_setpoints = self._split_setpoint(combined_setpoint_kw)

        return {
            "optimization_result": result,
            "battery_state": battery_state,
            "per_battery_states": dict(self._per_battery_states),
            "control_action": control_action,
            "battery_setpoints": battery_setpoints,
            "control_mode": self._control_mode,
            "optimal_power_kw": effective_power,
            "optimal_mode": effective_mode,
            "schedule_power_kw": result.optimal_power_kw,
            "schedule_mode": result.optimal_mode,
            "power_schedule_kw": result.power_schedule_kw,
            "mode_schedule": result.mode_schedule,
            "soc_schedule_kwh": result.soc_schedule_kwh,
            "step_durations_hours": step_durations_hours[:n_steps],
            "step_start_times_iso": step_start_times_iso[:n_steps],
            "total_cost": result.total_cost,
            "baseline_cost": result.baseline_cost,
            "savings": round(result.savings, 2),
            "shadow_price_eur_kwh": round(result.shadow_price_eur_kwh, 4),
            "raw_total_cost": result.raw_total_cost,
            "raw_savings": result.raw_savings,
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
            "price_forecast_model": price_forecast_model,
            "feed_in_price_forecast_model": feed_in_price_forecast_model,
            "feed_in_price_forecast": resampled_feed_in,
            "price_interval": price_interval,
            "timestamp": dt_util.utcnow(),
        }

"""Optimization coordinator for the Battery Controller integration."""

from __future__ import annotations

import asyncio
import logging
import math
from collections import deque
from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant, Event, EventStateChangedData, callback
from homeassistant.helpers import issue_registry as ir, storage
from homeassistant.helpers.event import (
    async_track_point_in_time,
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .battery_model import BatteryConfig, BatteryState, aggregate_battery_configs
from .const import (
    DOMAIN,
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
    CONF_PV_DC_COUPLED,
    CONF_PV_DC_PEAK_POWER_KWP,
    CONF_ZERO_GRID_DEADBAND_W,
    CONF_ZERO_GRID_RESPONSE_TIME_S,
    DEFAULT_ZERO_GRID_DEADBAND_W,
    DEFAULT_ZERO_GRID_RESPONSE_TIME_S,
    MODE_FOLLOW_SCHEDULE,
    MODE_HYBRID,
    MODE_HYBRID_PLUS,
    MODE_MANUAL,
    MODE_ZERO_GRID,
    PRICE_CHANGE_REOPTIMIZE_ABS_EUR,
    PRICE_CHANGE_REOPTIMIZE_THRESHOLD,
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

# Multi-battery dispatch: SoC-gap thresholds
# When the relative-SoC gap between batteries is below _SOC_SPLIT_THRESHOLD,
# concentrate the full setpoint on one battery (better tracking, avoids
# low-power inefficiency). Above the threshold, split proportionally to
# rebalance diverging SoC levels.
_SOC_SPLIT_THRESHOLD = 0.10
# Minimum rel-SoC advantage for a challenger battery to displace the current
# active battery within concentration mode. Must be < _SOC_SPLIT_THRESHOLD so
# there is a middle zone [_SOC_HYSTERESIS, _SOC_SPLIT_THRESHOLD) where a switch
# to a clearly better battery happens before proportional splitting kicks in.
_SOC_HYSTERESIS = 0.05

# Hybrid mode: when the DP plans charging while the grid is exporting, switch
# to zero_grid (surplus-following) only if the export surplus covers at least
# this fraction of the planned charge power. Below it, the DP evidently wants
# grid charging beyond the surplus and the schedule is followed instead.
_SURPLUS_COVERS_PLAN_FRACTION = 0.8


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
        self._apply_dc_pv_config()

        # Per-battery state cache (updated by get_current_battery_state)
        self._per_battery_states: dict[str, BatteryState] = {}

        # Active battery for concentration dispatch (None = select fresh next call)
        # zero_grid: one battery for both charge and discharge (stable across direction changes)
        # scheduled: separate tracker; direction determines selection criterion
        self._zero_grid_active_battery: str | None = None
        self._scheduled_active_battery: str | None = None

        # Zero-grid controller
        self.zero_grid_controller = create_zero_grid_controller(
            config, self.battery_config
        )

        # Control mode (restore from config or use default)
        self._control_mode: str = str(
            config.get(CONF_CONTROL_MODE, DEFAULT_CONTROL_MODE)
        )

        # Price sensor tracking
        self._price_sensor = config.get(CONF_PRICE_SENSOR)
        self._unsub_price: Any | None = None
        self._last_price: float | None = None

        # Feed-in price sensor tracking (separate entity from the buy price
        # sensor, so it needs its own period-boundary tracking to keep
        # current_feed_in_price — and PVCurtailmentSensor — in sync).
        self._feed_in_price_sensor = config.get(CONF_FEED_IN_PRICE_SENSOR)
        self._unsub_feed_in_price: Any | None = None
        self._last_feed_in_period_start: datetime | None = None

        # Real-time sensors for zero_grid control (grid power only; battery sensors in subentries)
        self._power_consumption_sensors = config.get(CONF_POWER_CONSUMPTION_SENSORS, [])
        self._power_production_sensors = config.get(CONF_POWER_PRODUCTION_SENSORS, [])
        self._unsub_realtime: Any | None = None
        # Sensors already warned about for unexpected units (avoid log spam)
        self._warned_sensor_units: set[str] = set()

        # First SoC sensor from any battery subentry (used for availability tracking)
        self._battery_soc_sensor: str | None = (
            self._battery_subentries[0][1].get(CONF_BATTERY_SOC_SENSOR)
            if self._battery_subentries
            else None
        )

        # Last optimization result and effective mode (persists between real-time updates)
        self._last_result: OptimizationResult | None = None
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

        # PV curtailment flag: when True the optimizer receives zeroed PV forecasts
        # and zero_grid mode follows the DP schedule instead of the live grid sensor.
        # Intended for use when the solar inverter curtails production at negative prices.
        self._pv_curtailed: bool = False

        # Guard against concurrent optimizer runs (e.g. price change + timer overlap).
        self._optimization_running: bool = False

        # When a trigger arrives while an optimization is already running, queue one
        # re-run rather than dropping the request entirely (P3.2).
        self._pending_optimization: bool = False

        # Human-readable label for what triggered the current/pending optimization
        # run. Set before each async_request_refresh() call; recorded in the run log.
        self._optimization_trigger_source: str = "unknown"

        # Last hybrid mode decision for hysteresis (P3.1).
        # Tracks whether we were in "discharging" (schedule) or "zero_grid" state
        # so small oscillations around the shadow-price threshold are damped.
        self._last_hybrid_decision: str = "zero_grid"

        # Hysteresis state for the hybrid idle branch: tracks whether we were in
        # "idle" or "zero_grid" so grid power hovering near 0 W doesn't flip the
        # mode every realtime-update tick.
        self._last_hybrid_idle_decision: str = ACTION_IDLE

        # Hysteresis state for the hybrid charging branch: tracks whether we were
        # following the DP schedule ("charging") or capturing PV surplus only
        # ("zero_grid") so surplus hovering near the coverage threshold doesn't
        # flip the mode every realtime-update tick.
        self._last_hybrid_charge_decision: str = ACTION_CHARGING

        # Hysteresis state for the hybrid+ surplus-capture decision: tracks
        # whether the last decision was to store PV surplus ("zero_grid") or
        # export it ("idle") so a shadow price hovering near the feed-in price
        # doesn't flip the mode every optimization run.
        self._last_hybrid_plus_capture_decision: str = "zero_grid"

        # Whether hybrid+ currently blocks PV-surplus capture (exporting at the
        # current feed-in price is worth more than storing). Gates the
        # realtime idle→zero_grid upgrade in _resolve_controller_mode so a
        # surplus appearing between optimizer runs isn't captured anyway.
        self._hybrid_plus_capture_blocked: bool = False

        # Diagnostic history ring buffers (not persisted across restarts).
        # optimizer_run_log: one entry per 15-min optimizer run (24 h @ 15 min = 96).
        # setpoint_log: one entry every time the real-time setpoint changes.
        self._optimizer_run_log: deque[dict[str, Any]] = deque(maxlen=96)
        self._setpoint_log: deque[dict[str, Any]] = deque(maxlen=576)

        # Charge efficiency calibration: rolling window of (actual_delta / planned_delta)
        # samples collected during active charging steps. The smoothed correction
        # is applied as a multiplier on the DP's charge-side SoC transition only,
        # to account for systematic over-estimation of how much SoC can be added
        # within one step (e.g. CV-phase derating near full SoC). It does not
        # change the economic cost model. Only non-DC-coupled systems are sampled
        # to avoid confounding with passive PV.
        self._charge_eff_samples: deque[float] = deque(maxlen=20)
        self._charge_eff_correction: float = 1.0

        # Persistent storage for charge efficiency calibration (survives reboots).
        entry_id = config.get("entry_id", "unknown")
        self._charge_eff_store: storage.Store[dict[str, Any]] = storage.Store(
            hass, 1, f"battery_controller_{entry_id}_charge_eff"
        )

        # Discharge efficiency calibration: rolling window of (actual_delta / planned_delta)
        # samples collected during active discharging steps. The smoothed correction
        # is applied as a multiplier on the DP's discharge-side SoC transition only,
        # to account for systematic over-estimation of how much SoC can be removed
        # within one step. It does not change the economic cost model.
        self._discharge_eff_samples: deque[float] = deque(maxlen=20)
        self._discharge_eff_correction: float = 1.0

        # Persistent storage for discharge efficiency calibration (survives reboots).
        self._discharge_eff_store: storage.Store[dict[str, Any]] = storage.Store(
            hass, 1, f"battery_controller_{entry_id}_discharge_eff"
        )

    @property
    def control_mode(self) -> str:
        """Get current control mode."""
        return self._control_mode

    @control_mode.setter
    def control_mode(self, mode: str) -> None:
        """Set control mode and reset commitment state to prevent stale locks."""
        if mode == self._control_mode:
            # Re-selecting the active mode (e.g. an automation periodically
            # calling select.select_option) must not reset the commitment
            # filter or force the real-time loop through an idle setpoint —
            # nothing changed, so there is no stale state to clear.
            return
        self._control_mode = mode
        self._committed_action = ACTION_IDLE
        self._committed_price = 0.0
        self._committed_power = 0.0
        self._committed_step_start = None
        self._last_hybrid_plus_capture_decision = "zero_grid"
        self._hybrid_plus_capture_blocked = False
        # Reset cached setpoint so the real-time loop uses idle immediately
        # instead of applying stale setpoints from the previous mode while the
        # re-optimization triggered by the mode change is still running.
        self._effective_mode = ACTION_IDLE
        self._controller_schedule_w = 0.0
        self._optimization_trigger_source = "mode_change"

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

    @property
    def pv_curtailed(self) -> bool:
        """Return whether PV curtailment mode is active."""
        return self._pv_curtailed

    @pv_curtailed.setter
    def pv_curtailed(self, value: bool) -> None:
        """Enable or disable PV curtailment mode."""
        self._pv_curtailed = value

    async def _handle_price_model_refresh(self, now: datetime) -> None:
        """Refresh historical price model from HA recorder (daily timer)."""
        _LOGGER.debug("Daily price model refresh triggered at %s", now)
        await asyncio.gather(
            self._price_model.async_update_pattern(),
            self._feed_in_price_model.async_update_pattern(),
        )

    async def async_setup(self) -> None:
        """Set up event tracking for price changes and real-time control."""
        await asyncio.gather(
            self._price_model.async_update_pattern(),
            self._feed_in_price_model.async_update_pattern(),
            self._async_load_charge_eff_calibration(),
            self._async_load_discharge_eff_calibration(),
        )

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

        if (
            self._feed_in_price_sensor
            and self._feed_in_price_sensor != self._price_sensor
        ):
            self._unsub_feed_in_price = async_track_state_change_event(
                self.hass,
                [self._feed_in_price_sensor],
                self._handle_feed_in_price_change,
            )
            _LOGGER.debug(
                "Tracking feed-in price sensor: %s", self._feed_in_price_sensor
            )

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
                self._optimization_trigger_source = "forecast_available"
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
            self._optimization_trigger_source = "price_available"
            self.hass.async_create_task(self.async_request_refresh())
        elif period_start is not None and period_start != self._last_period_start:
            # New price period — primary optimization trigger.
            _LOGGER.debug(
                "New price period started at %s, triggering optimization", period_start
            )
            self._last_price = new_price
            self._last_period_start = period_start
            self._schedule_mid_period_run(period_start, interval_minutes)
            self._optimization_trigger_source = "price_boundary"
            self.hass.async_create_task(self.async_request_refresh())
        elif self._last_price is not None:
            # Same period or no timestamp info — fallback threshold check.
            if abs(self._last_price) > 1e-9:
                change_pct = abs(new_price - self._last_price) / abs(self._last_price)
                significant = change_pct >= PRICE_CHANGE_REOPTIMIZE_THRESHOLD
            else:
                # Relative change is undefined at a zero price (free hour):
                # fall back to an absolute threshold so the trigger does not
                # go dead — previously the price also never updated from 0,
                # permanently disabling this fallback path.
                significant = (
                    abs(new_price - self._last_price) >= PRICE_CHANGE_REOPTIMIZE_ABS_EUR
                )
            if significant:
                _LOGGER.debug(
                    "Significant price change: %.4f -> %.4f, triggering optimization",
                    self._last_price,
                    new_price,
                )
                self._optimization_trigger_source = "price_spike"
                self.hass.async_create_task(self.async_request_refresh())
            self._last_price = new_price

    @callback
    def _handle_feed_in_price_change(self, event: Event[EventStateChangedData]) -> None:
        """Handle feed-in price sensor state changes.

        When the feed-in price is a separate sensor from the buy price, its
        period boundaries are not covered by _handle_price_change. Without
        this listener, current_feed_in_price (and PVCurtailmentSensor) would
        only refresh when the buy price sensor happens to change, the
        mid-period timer fires, or the 60-min base update_interval elapses —
        so it can lag the actual feed-in price by up to an hour.
        """
        new_state = event.data.get("new_state")
        if not new_state:
            return

        try:
            float(new_state.state)
        except (ValueError, TypeError):
            return  # Sensor is unavailable/unknown, ignore

        old_state = event.data.get("old_state")
        was_unavailable = (
            self._last_feed_in_period_start is None
            or old_state is None
            or old_state.state in ("unknown", "unavailable")
        )

        try:
            _, start_times, _ = extract_price_forecast_with_timestamps(new_state)
            period_start: datetime | None = start_times[0] if start_times else None
        except Exception:  # noqa: BLE001
            period_start = None

        if was_unavailable or (
            period_start is not None and period_start != self._last_feed_in_period_start
        ):
            _LOGGER.debug(
                "Feed-in price period changed at %s, triggering optimization",
                period_start,
            )
            self._last_feed_in_period_start = period_start
            self._optimization_trigger_source = "feed_in_price_change"
            self.hass.async_create_task(self.async_request_refresh())

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
            self._optimization_trigger_source = "mid_period"
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
            self._optimization_trigger_source = "soc_available"
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

        # Stale sensor detection (P4.1): if any contributing grid sensor has
        # not reported recently its reading may be unreliable. The check covers
        # all configured power sensors and uses last_reported, so a live push
        # sensor holding a steady value (e.g. production at 0 W overnight) is
        # not flagged. On-change-only sources (template/MQTT) do stop reporting
        # whenever the grid is steady — including a grid held steady by the
        # battery's own action. We therefore do NOT unconditionally skip:
        # misclassifying that as "stale" and skipping would freeze the
        # controller in an over-committed setpoint (e.g. the battery left
        # discharging after a Quooker/kettle spike), which only the next full
        # optimizer run would clear. Instead the flag is handled per-mode below.
        stale_limit_s = STALE_SENSOR_MULTIPLIER * float(
            self.config.get(
                CONF_ZERO_GRID_RESPONSE_TIME_S, DEFAULT_ZERO_GRID_RESPONSE_TIME_S
            )
        )
        grid_sensor_stale = self._find_stale_power_sensor(stale_limit_s) is not None

        # Read current battery state
        battery_state = self.get_current_battery_state()

        # Re-derive effective mode from the current control mode so that a
        # mode switch (e.g. hybrid → follow_schedule) takes effect in the
        # real-time loop immediately, without waiting for the next 15-min run.
        if self._control_mode == MODE_ZERO_GRID:
            if self._pv_curtailed:
                # Follow the cached DP schedule (set during the last 15-min run)
                # instead of the live grid sensor — same logic as hybrid/follow_schedule.
                rt_effective_mode = self._effective_mode
                controller_schedule_w = self._controller_schedule_w
            else:
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

        # A stale grid reading is only relevant to grid-driven control. For
        # follow_schedule / manual / idle the setpoint comes from the DP schedule
        # or the user, not the live grid, so holding the last setpoint (as before)
        # is correct and safe.
        if grid_sensor_stale and controller_mode != "zero_grid":
            _LOGGER.debug(
                "Grid power sensor stale (>%.0f s); holding %s setpoint",
                stale_limit_s,
                controller_mode,
            )
            return

        # Recalculate zero_grid setpoint with actual sensor data
        saved_last_target_w = self.zero_grid_controller.last_target_w
        control_action = self.zero_grid_controller.get_control_action(
            current_grid_w=current_grid_w,
            current_soc_kwh=battery_state.soc_kwh,
            current_battery_w=battery_state.power_kw * 1000,
            dp_schedule_w=controller_schedule_w,
            mode=controller_mode,
        )

        # Stale sensor in zero_grid: the live grid drives the setpoint. Acting on
        # a steady reading is exactly what breaks a self-locking over-commitment
        # (reducing battery power changes the grid and wakes an on-change sensor).
        # But a genuinely dead sensor stays frozen, so only allow the setpoint to
        # move TOWARD zero — never let a stale reading push the battery into a
        # larger charge/discharge or flip its direction (no runaway).
        if grid_sensor_stale:
            prev_w = (
                self.data.get("control_action", {}).get("target_power_w", 0.0)
                if self.data
                else 0.0
            )
            new_w = control_action["target_power_w"]
            moves_away_from_zero = abs(new_w) > abs(prev_w) or (new_w * prev_w < 0)
            if moves_away_from_zero:
                # Untrustworthy while stale — restore integrator state and hold.
                self.zero_grid_controller.reset_setpoint(saved_last_target_w)
                _LOGGER.debug(
                    "Grid power sensor stale; holding zero_grid setpoint "
                    "(rejected %.0f W -> %.0f W, away from zero)",
                    prev_w,
                    new_w,
                )
                return

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
        # Append to real-time setpoint history for diagnostics
        self._setpoint_log.append(
            {
                "timestamp": dt_util.now().isoformat(),
                "schedule_kw": round(controller_schedule_w / 1000, 3),
                "setpoint_kw": round(control_action["target_power_kw"], 3),
                "raw_target_kw": round(control_action["raw_target_w"] / 1000, 3),
                "mode": control_action["mode"],
                "effective_mode": rt_effective_mode,
                "soc_kwh": round(battery_state.soc_kwh, 3),
                "soc_percent": round(battery_state.soc_percent, 1),
                "grid_kw": round(current_grid_w / 1000, 3),
                "battery_kw": round(battery_state.power_kw, 3),
                "soc_limited": (
                    abs(control_action["dp_schedule_w"]) > 50
                    and abs(control_action["raw_target_w"])
                    < abs(control_action["dp_schedule_w"]) - 50
                ),
            }
        )

        battery_setpoints = self._split_setpoint(
            control_action["target_power_kw"], control_action["mode"]
        )
        self.async_set_updated_data(
            {
                **self.data,
                "control_action": control_action,
                "battery_state": battery_state,
                "per_battery_states": dict(self._per_battery_states),
                "battery_setpoints": battery_setpoints,
                "optimal_power_kw": control_action["target_power_kw"],
                # Report the effective control mode (e.g. "zero_grid"), matching
                # what the full optimizer run publishes. Using the instantaneous
                # physical action here would flip the "Optimal Mode" sensor to
                # "discharging"/"charging" between optimizer runs whenever the
                # zero-grid loop moves the battery — even though the control mode
                # is still zero_grid. The physical action is already visible via
                # the Battery Power / Setpoint sensors.
                "optimal_mode": rt_effective_mode,
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
        entry = self.hass.config_entries.async_get_entry(entry_id)
        if entry is None:
            return DEFAULT_MANUAL_POWER_SETPOINT_W
        stored = float(
            entry.options.get(
                CONF_MANUAL_POWER_SETPOINT_W, DEFAULT_MANUAL_POWER_SETPOINT_W
            )
        )
        # Negate: user enters positive=discharge, controller expects positive=charge
        return -stored

    def _hybrid_plus_should_capture_surplus(
        self, shadow_price_eur_kwh: float, current_feed_in: float
    ) -> bool:
        """Return whether hybrid+ should store PV surplus rather than export it.

        Storing 1 kWh of AC surplus puts sqrt(RTE) kWh in the battery, each
        worth the DP shadow price λ — the marginal value of stored energy given
        the full price and PV forecast. Exporting the same kWh yields the
        current feed-in price. When λ × sqrt(RTE) is below the feed-in price,
        the forecast says the battery can be filled more cheaply later (e.g.
        the midday PV peak at low prices), so exporting now is worth more than
        storing.

        Applies a ±5% hysteresis band around the feed-in threshold (same
        pattern as the hybrid discharge decision) so a shadow price hovering
        near the feed-in price doesn't flip the decision every run.
        """
        if current_feed_in <= 0:
            # Exporting earns nothing (or costs money): always capture.
            self._last_hybrid_plus_capture_decision = "zero_grid"
            return True
        sqrt_rte = float(self.battery_config.round_trip_efficiency) ** 0.5
        store_value = shadow_price_eur_kwh * sqrt_rte
        if self._last_hybrid_plus_capture_decision == "zero_grid":
            should_capture = bool(store_value >= current_feed_in * 0.95)
        else:
            should_capture = bool(store_value >= current_feed_in * 1.05)
        self._last_hybrid_plus_capture_decision = (
            "zero_grid" if should_capture else ACTION_IDLE
        )
        return should_capture

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

        In hybrid+ mode the upgrade is suppressed while surplus capture is
        blocked (exporting is worth more than storing per the shadow price),
        so idle genuinely means "export the surplus".

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
        # Upgrade idle → zero_grid only in zero_grid/hybrid/hybrid+ modes (not
        # follow_schedule or manual, where idle must mean truly stop). Only when
        # grid is actually exporting (negative), i.e. real PV surplus — not just
        # near-zero import noise. Hybrid+ additionally requires that surplus
        # capture is economical per the last optimizer run.
        if (
            effective_mode == ACTION_IDLE
            and self._control_mode not in (MODE_FOLLOW_SCHEDULE, MODE_MANUAL)
            and not (
                self._control_mode == MODE_HYBRID_PLUS
                and self._hybrid_plus_capture_blocked
            )
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

    def _find_stale_power_sensor(
        self, stale_limit_s: float
    ) -> tuple[str, float] | None:
        """Return the first available-but-stale realtime power sensor, or None.

        Only sensors that currently report a value are checked: an unavailable
        sensor is already excluded from the grid sum, but an available sensor
        that silently stopped reporting keeps feeding an outdated value into it.

        Uses last_reported rather than last_updated: a live push sensor (e.g.
        DSMR) keeps reporting even when the value is constant — such as a
        production sensor reading 0 W all night — and last_updated only
        advances when the value actually changes, which would flag every
        steady sensor as stale.
        """
        now = dt_util.utcnow()
        for sensor_id in (
            *self._power_consumption_sensors,
            *self._power_production_sensors,
        ):
            state = self.hass.states.get(sensor_id)
            if state is None or state.state in ("unknown", "unavailable"):
                continue
            reported = state.last_reported or state.last_updated
            age_s = (now - reported).total_seconds()
            if age_s > stale_limit_s:
                return sensor_id, age_s
        return None

    def _get_realtime_grid_w(self) -> float | None:
        """Read current grid power from DSMR power sensors.

        Calculates grid power as: sum(consumption) - sum(production).

        Note: DSMR sensors already include battery power in their readings:
        - consumption = household + battery_charging
        - production = PV - battery_discharging (or + depending on config)
        So the result already reflects the net grid flow including battery impact.
        We don't need to subtract battery_power separately.

        Returns:
            Grid power in W (positive = import), or None if no sensors are
            configured or none of the configured sensors has a usable reading
            (callers then fall back to the forecast-based estimate instead of
            acting on a fictitious 0 W).
        """
        if not (self._power_consumption_sensors or self._power_production_sensors):
            return None

        total_consumption = 0.0
        total_production = 0.0
        got_reading = False

        # Sum all consumption sensors
        for sensor_id in self._power_consumption_sensors:
            value = self._read_power_sensor_w(sensor_id)
            if value is not None:
                total_consumption += value
                got_reading = True

        # Sum all production sensors
        for sensor_id in self._power_production_sensors:
            value = self._read_power_sensor_w(sensor_id)
            if value is not None:
                total_production += value
                got_reading = True

        if not got_reading:
            return None

        return total_consumption - total_production

    def _read_power_sensor_w(self, sensor_id: str) -> float | None:
        """Read one power sensor and convert its value to W.

        Sensors without a unit attribute are assumed to report W. Sensors with
        an unrecognized power unit are skipped (with a one-time warning):
        silently assuming W would be off by orders of magnitude for e.g. MW.
        """
        state = self.hass.states.get(sensor_id)
        if not state or state.state in ("unknown", "unavailable"):
            return None
        try:
            value = float(state.state)
        except (ValueError, TypeError):
            return None
        unit = state.attributes.get("unit_of_measurement", "W")
        if unit in ("W", ""):
            return value
        if unit == "kW":
            return value * 1000
        self._warn_unit_once(sensor_id, unit, "ignoring sensor")
        return None

    def _warn_unit_once(self, sensor_id: str, unit: str, action: str) -> None:
        """Warn about an unexpected sensor unit, once per sensor."""
        if sensor_id in self._warned_sensor_units:
            return
        self._warned_sensor_units.add(sensor_id)
        _LOGGER.warning(
            "Sensor '%s' has unexpected unit '%s'; %s", sensor_id, unit, action
        )

    async def async_shutdown(self) -> None:
        """Clean up event tracking."""
        if self._unsub_price:
            self._unsub_price()
            self._unsub_price = None
        if self._unsub_feed_in_price:
            self._unsub_feed_in_price()
            self._unsub_feed_in_price = None
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
        await super().async_shutdown()

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
                if unit in ("kWh", "Wh"):
                    soc_kwh = soc_value / 1000 if unit == "Wh" else soc_value
                    soc_percent = (
                        (soc_kwh / battery_config.capacity_kwh) * 100
                        if battery_config.capacity_kwh > 0
                        else 0.0
                    )
                else:
                    if unit not in ("%", ""):
                        self._warn_unit_once(
                            soc_sensor, unit, "treating value as percent"
                        )
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
                    self._warn_unit_once(power_sensor, unit, "treating value as kW")

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

    def _split_setpoint(self, total_kw: float, mode: str = "") -> dict[str, float]:
        """Split combined setpoint (kW, positive=charge) to per-battery setpoints.

        Uses SoC-gap triggered concentration:
        - Gap < _SOC_SPLIT_THRESHOLD: concentrate on one battery. Avoids
          splitting tiny setpoints across inverters and provides stable
          single-inverter tracking for zero-grid corrections.
        - Gap >= _SOC_SPLIT_THRESHOLD: proportional split with iterative
          power-overflow redistribution to rebalance diverging SoC levels.

        Battery selection for concentration (with _SOC_HYSTERESIS to prevent
        rapid switching):
        - zero_grid / hybrid / hybrid+: battery closest to 50% rel_soc — handles both
          charge and discharge direction changes without switching inverters.
        - scheduled charge: battery with lowest rel_soc (most headroom).
        - scheduled discharge: battery with highest rel_soc (most energy).
        """
        if not self._individual_battery_configs:
            return {}
        if abs(total_kw) < 1e-6:
            return {sid: 0.0 for sid, _ in self._individual_battery_configs}

        rel_socs = self._compute_rel_socs()
        soc_gap = max(rel_socs.values()) - min(rel_socs.values())

        if soc_gap >= _SOC_SPLIT_THRESHOLD:
            # SoC has diverged — proportional split to rebalance; reset active batteries
            self._zero_grid_active_battery = None
            self._scheduled_active_battery = None
            return self._proportional_split(total_kw, self._individual_battery_configs)

        winner = self._select_active_battery(total_kw, rel_socs, mode)
        return self._concentrate(total_kw, winner)

    def _compute_rel_socs(self) -> dict[str, float]:
        """Compute relative SoC position [0, 1] per battery within its usable range."""
        result: dict[str, float] = {}
        for sid, cfg in self._individual_battery_configs:
            usable = cfg.max_soc_kwh - cfg.min_soc_kwh
            if usable > 0 and sid in self._per_battery_states:
                soc = self._per_battery_states[sid].soc_kwh
                result[sid] = (soc - cfg.min_soc_kwh) / usable
            else:
                result[sid] = 0.5
        return result

    def _select_active_battery(
        self, total_kw: float, rel_socs: dict[str, float], mode: str
    ) -> str:
        """Select which battery to concentrate the setpoint on.

        Applies hysteresis: only displaces the current active battery when the
        best candidate's score exceeds the current battery's by _SOC_HYSTERESIS.
        """
        sids = [sid for sid, _ in self._individual_battery_configs]

        if mode in (MODE_ZERO_GRID, MODE_HYBRID, MODE_HYBRID_PLUS):
            # Prefer battery closest to 50% rel_soc: stays within limits longest
            # regardless of whether the next setpoint is charge or discharge.
            def score(sid: str) -> float:
                return -abs(rel_socs[sid] - 0.5)

            active_attr = "_zero_grid_active_battery"
        elif total_kw > 0:
            # Scheduled charge: prefer lowest rel_soc (most room)
            def score(sid: str) -> float:
                return -rel_socs[sid]

            active_attr = "_scheduled_active_battery"
        else:
            # Scheduled discharge: prefer highest rel_soc (most energy)
            def score(sid: str) -> float:
                return rel_socs[sid]

            active_attr = "_scheduled_active_battery"

        best = max(sids, key=score)
        current: str | None = getattr(self, active_attr)

        if current is not None and current in rel_socs:
            if score(best) - score(current) <= _SOC_HYSTERESIS:
                best = current  # stay with current battery

        setattr(self, active_attr, best)
        return best

    def _concentrate(self, total_kw: float, winner: str) -> dict[str, float]:
        """Send full setpoint to winner; redistribute overflow above max_power to others."""
        result: dict[str, float] = {
            sid: 0.0 for sid, _ in self._individual_battery_configs
        }
        winner_cfg = next(
            cfg for sid, cfg in self._individual_battery_configs if sid == winner
        )
        winner_state = self._per_battery_states.get(winner)
        winner_soc_kwh = (
            winner_state.soc_kwh
            if winner_state is not None
            else (winner_cfg.min_soc_kwh + winner_cfg.max_soc_kwh) / 2
        )

        others = [
            (sid, cfg) for sid, cfg in self._individual_battery_configs if sid != winner
        ]
        if total_kw > 0:
            headroom = max(0.0, winner_cfg.max_soc_kwh - winner_soc_kwh)
            if headroom <= 0:
                # Winner is full (can happen via selection hysteresis): hand
                # the whole setpoint to the remaining batteries instead of
                # dropping it.
                if others:
                    for sid, val in self._proportional_split(total_kw, others).items():
                        result[sid] = val
                return result
            clamped = min(total_kw, winner_cfg.max_charge_at_soc(winner_soc_kwh))
        else:
            available = max(0.0, winner_soc_kwh - winner_cfg.min_soc_kwh)
            if available <= 0:
                # Winner is empty: redistribute the discharge to the others.
                if others:
                    for sid, val in self._proportional_split(total_kw, others).items():
                        result[sid] = val
                return result
            clamped = max(total_kw, -winner_cfg.max_discharge_at_soc(winner_soc_kwh))

        result[winner] = clamped
        overflow = total_kw - clamped
        if abs(overflow) > 1e-6:
            if others:
                for sid, val in self._proportional_split(overflow, others).items():
                    result[sid] = val
        return result

    def _proportional_split(
        self,
        total_kw: float,
        configs: list[tuple[str, BatteryConfig]],
    ) -> dict[str, float]:
        """Proportional split with iterative overflow redistribution.

        Splits total_kw proportionally to available headroom (charging) or
        available energy (discharging). Batteries that hit their max_power
        limit have the overflow redistributed to the remaining batteries.
        Iterates at most len(configs) rounds until all overflow is absorbed
        or no capacity remains.
        """
        result: dict[str, float] = {}
        remaining_kw = total_kw
        remaining = list(configs)

        for _ in range(len(configs)):
            if not remaining or abs(remaining_kw) < 1e-6:
                break

            if remaining_kw > 0:
                weights = {
                    sid: max(
                        0.0,
                        cfg.max_soc_kwh - self._per_battery_states[sid].soc_kwh,
                    )
                    if sid in self._per_battery_states
                    else cfg.max_soc_kwh * 0.5
                    for sid, cfg in remaining
                }
            else:
                weights = {
                    sid: max(
                        0.0,
                        self._per_battery_states[sid].soc_kwh - cfg.min_soc_kwh,
                    )
                    if sid in self._per_battery_states
                    else cfg.capacity_kwh * 0.4
                    for sid, cfg in remaining
                }

            total_weight = sum(weights.values())
            overflow = 0.0
            next_remaining: list[tuple[str, BatteryConfig]] = []

            for sid, cfg in remaining:
                raw = (
                    remaining_kw * weights[sid] / total_weight
                    if total_weight > 0
                    else remaining_kw / len(remaining)
                )
                state = self._per_battery_states.get(sid)
                soc_kwh = (
                    state.soc_kwh
                    if state is not None
                    else (cfg.min_soc_kwh + cfg.max_soc_kwh) / 2
                )
                if remaining_kw > 0:
                    max_chg = cfg.max_charge_at_soc(soc_kwh)
                    clamped = min(raw, max_chg)
                    at_limit = clamped >= max_chg - 1e-6
                else:
                    max_dchg = cfg.max_discharge_at_soc(soc_kwh)
                    clamped = max(raw, -max_dchg)
                    at_limit = clamped <= -max_dchg + 1e-6

                result[sid] = result.get(sid, 0.0) + clamped
                overflow += raw - clamped
                if not at_limit:
                    next_remaining.append((sid, cfg))

            remaining_kw = overflow
            remaining = next_remaining

        for sid, _ in configs:
            result.setdefault(sid, 0.0)
        return result

    def _refresh_battery_config(self) -> None:
        """Re-read BatteryConfigs from live battery subentry data.

        Called at the start of each optimization run so that SoC limit changes
        made in the subentry config flow take effect without a full reload.
        """
        entry_id = self.config.get("entry_id", "")
        entry = self.hass.config_entries.async_get_entry(entry_id)
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
        self._apply_dc_pv_config()

    def _apply_dc_pv_config(self) -> None:
        """Overlay entry-level DC-PV configuration onto the aggregated battery config.

        DC coupling is configured on the PV-array subentries, not on the battery
        subentries, so BatteryConfig.from_subentry can never set pv_dc_coupled.
        Without this overlay the optimizer always sees pv_dc_coupled=False and
        never models passive DC MPPT charging.
        """
        if not self.config.get(CONF_PV_DC_COUPLED):
            return
        self.battery_config.pv_dc_coupled = True
        self.battery_config.pv_dc_peak_power_kwp = float(
            self.config.get(CONF_PV_DC_PEAK_POWER_KWP, 0.0)
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
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="optimization_in_progress",
            )

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
                self._optimization_trigger_source = "pending_rerun"
                self.hass.async_create_task(self.async_request_refresh())

    async def _async_load_charge_eff_calibration(self) -> None:
        """Load persisted charge efficiency calibration from storage."""
        stored = await self._charge_eff_store.async_load()
        if stored is None:
            return
        samples = stored.get("samples", [])
        correction = stored.get("correction", 1.0)
        self._charge_eff_samples = deque(samples, maxlen=20)
        self._charge_eff_correction = float(correction)
        if self._charge_eff_correction < 0.995:
            _LOGGER.info(
                "Restored charge efficiency calibration: correction=%.3f, n=%d samples",
                self._charge_eff_correction,
                len(self._charge_eff_samples),
            )

    async def _async_save_charge_eff_calibration(self) -> None:
        """Persist current charge efficiency calibration to storage."""
        await self._charge_eff_store.async_save(
            {
                "samples": list(self._charge_eff_samples),
                "correction": self._charge_eff_correction,
            }
        )

    async def async_reset_charge_eff_calibration(self) -> None:
        """Reset charge-efficiency calibration to the nominal uncorrected state."""
        if self._charge_eff_samples or abs(self._charge_eff_correction - 1.0) > 1e-9:
            _LOGGER.info(
                "Resetting charge efficiency calibration: %.3f (%d samples) -> 1.000",
                self._charge_eff_correction,
                len(self._charge_eff_samples),
            )
        self._charge_eff_samples.clear()
        self._charge_eff_correction = 1.0
        await self._async_save_charge_eff_calibration()

    def _previous_charge_step_complete(self) -> bool:
        """Return whether the previously planned first step has elapsed.

        Charge-efficiency calibration compares the current SoC against the
        previous optimizer run's first planned charging step. That comparison is
        only valid once the entire scheduled step has finished; otherwise we
        would compare a partial real-world step against a full-step plan and
        systematically underestimate efficiency.

        If timing metadata is unavailable or unparsable, fall back to the
        previous behaviour and allow calibration rather than silently disabling
        it.
        """
        if not isinstance(self.data, dict):
            return True

        step_start_times = self.data.get("step_start_times_iso")
        step_durations = self.data.get("step_durations_hours")
        if not step_start_times or not step_durations:
            return True

        step_start_raw = step_start_times[0]
        if isinstance(step_start_raw, str):
            step_start = dt_util.parse_datetime(step_start_raw)
        elif isinstance(step_start_raw, datetime):
            step_start = step_start_raw
        else:
            return True

        if step_start is None:
            return True

        try:
            step_duration_hours = float(step_durations[0])
        except (TypeError, ValueError, IndexError):
            return True

        step_end_utc = dt_util.as_utc(step_start) + timedelta(hours=step_duration_hours)
        return bool(dt_util.utcnow() >= step_end_utc)

    def _planned_first_step_was_executed(self, planned_action: str) -> bool:
        """Return whether the previous run's planned first step was commanded as-is.

        The DP plan is not always what gets executed: hybrid mode can resolve a
        planned charge/discharge to zero_grid, the commitment filter can lock a
        different power, and zero_grid/manual control modes ignore the schedule
        entirely. Efficiency calibration must only sample steps where the plan
        was sent to the controller unchanged — otherwise the actual SoC delta
        reflects the mode resolution, not battery efficiency.
        """
        if self._effective_mode != planned_action:
            return False
        if self._last_result is None or not self._last_result.power_schedule_kw:
            return False
        planned_kw = self._last_result.power_schedule_kw[0]
        executed_kw = self._controller_schedule_w / 1000
        tolerance_kw = max(0.05, 0.1 * abs(planned_kw))
        return abs(executed_kw - planned_kw) <= tolerance_kw

    def _update_charge_eff_calibration(self, battery_state: BatteryState) -> None:
        """Compare previous planned SoC to actual SoC and update charge efficiency correction.

        Samples are only collected when:
        - The previous optimizer step planned active charging (mode == 'charging')
        - The planned step was actually commanded unchanged (no hybrid/zero_grid
          override, no commitment-filter power lock)
        - The full planned step has elapsed
        - The planned SoC delta is large enough to be reliable (>= 0.1 kWh)
        - DC-coupled PV is not active (passive PV charging would inflate actual delta)
        - The step does not cross the high-SoC charge derating threshold (the DP
          plans at full power for the whole step, but the inverter throttles
          mid-step once the threshold is reached, making actual < planned even
          with perfect efficiency)

        The correction is the mean of the last 20 ratios (actual/planned), clipped to
        [0.5, 1.05]. It is applied as a multiplier on the DP's charge-side SoC
        transition so the optimizer plans less charge within one time step when
        the battery charges slower than modelled. It does not alter the economic
        cost calculation or degradation model.
        """
        if self._last_result is None:
            return
        if (
            len(self._last_result.mode_schedule) < 1
            or len(self._last_result.soc_schedule_kwh) < 2
        ):
            return

        if self._last_result.mode_schedule[0] != ACTION_CHARGING:
            return

        # Only sample when the plan was actually commanded; mode resolution
        # (hybrid → zero_grid, commitment filter, zero_grid/manual control)
        # would otherwise drag the correction towards the 0.5 clip floor.
        if not self._planned_first_step_was_executed(ACTION_CHARGING):
            return

        if not self._previous_charge_step_complete():
            return

        # Skip DC-coupled systems: passive PV charging during the step would
        # inflate actual_delta relative to the grid-only planned_delta.
        if self.config.get(CONF_PV_DC_COUPLED, False):
            return

        prev_soc = self._last_result.soc_schedule_kwh[0]
        planned_next_soc = self._last_result.soc_schedule_kwh[1]
        planned_delta = planned_next_soc - prev_soc

        if planned_delta < 0.1:
            # Too small to measure reliably; skip.
            return

        # Skip if the step crosses the high-SoC charge derating threshold.
        # The DP plans at the start-SoC limit for the whole step, but the
        # inverter throttles once the threshold is reached mid-step.  The
        # resulting actual/planned ratio reflects the derating, not real
        # efficiency losses, so including it would corrupt the calibration.
        bc = self.battery_config
        if bc.high_soc_max_charge_kw > 0:
            threshold_kwh = bc.high_soc_charge_threshold_pct / 100 * bc.capacity_kwh
            if prev_soc < threshold_kwh <= planned_next_soc:
                _LOGGER.debug(
                    "Charge efficiency calibration: skipping sample — step crosses "
                    "high-SoC derating threshold (%.1f%% / %.2f kWh)",
                    bc.high_soc_charge_threshold_pct,
                    threshold_kwh,
                )
                return

        actual_delta = battery_state.soc_kwh - prev_soc
        # Cap upward to 1.2× planned to filter out unexpected external charge
        # sources (e.g. brief grid export reversed, SoC sensor noise).
        actual_delta = max(0.0, min(actual_delta, 1.2 * planned_delta))
        ratio = actual_delta / planned_delta
        # Clip to a physically reasonable range; the correction should never
        # indicate the battery charged *more* than modelled by more than 5%.
        ratio = max(0.5, min(1.05, ratio))

        self._charge_eff_samples.append(ratio)
        new_correction = sum(self._charge_eff_samples) / len(self._charge_eff_samples)

        changed = abs(new_correction - self._charge_eff_correction) > 0.005
        if changed:
            _LOGGER.info(
                "Charge efficiency correction updated: %.3f → %.3f "
                "(latest ratio=%.3f, n=%d samples, planned Δ=%.2f kWh, actual Δ=%.2f kWh)",
                self._charge_eff_correction,
                new_correction,
                ratio,
                len(self._charge_eff_samples),
                planned_delta,
                actual_delta,
            )
        self._charge_eff_correction = new_correction
        if changed:
            self.hass.async_create_task(self._async_save_charge_eff_calibration())

    async def _async_load_discharge_eff_calibration(self) -> None:
        """Load persisted discharge efficiency calibration from storage."""
        stored = await self._discharge_eff_store.async_load()
        if stored is None:
            return
        samples = stored.get("samples", [])
        correction = stored.get("correction", 1.0)
        self._discharge_eff_samples = deque(samples, maxlen=20)
        self._discharge_eff_correction = float(correction)
        if self._discharge_eff_correction < 0.995:
            _LOGGER.info(
                "Restored discharge efficiency calibration: correction=%.3f, n=%d samples",
                self._discharge_eff_correction,
                len(self._discharge_eff_samples),
            )

    async def _async_save_discharge_eff_calibration(self) -> None:
        """Persist current discharge efficiency calibration to storage."""
        await self._discharge_eff_store.async_save(
            {
                "samples": list(self._discharge_eff_samples),
                "correction": self._discharge_eff_correction,
            }
        )

    async def async_reset_discharge_eff_calibration(self) -> None:
        """Reset discharge-efficiency calibration to the nominal uncorrected state."""
        if (
            self._discharge_eff_samples
            or abs(self._discharge_eff_correction - 1.0) > 1e-9
        ):
            _LOGGER.info(
                "Resetting discharge efficiency calibration: %.3f (%d samples) -> 1.000",
                self._discharge_eff_correction,
                len(self._discharge_eff_samples),
            )
        self._discharge_eff_samples.clear()
        self._discharge_eff_correction = 1.0
        await self._async_save_discharge_eff_calibration()

    def _update_discharge_eff_calibration(self, battery_state: BatteryState) -> None:
        """Compare previous planned SoC to actual SoC and update discharge efficiency correction.

        Samples are only collected when:
        - The previous optimizer step planned active discharging (mode == 'discharging')
        - The planned step was actually commanded unchanged (no hybrid/zero_grid
          override, no commitment-filter power lock)
        - The full planned step has elapsed
        - The planned SoC delta is large enough to be reliable (>= 0.1 kWh)
        - DC-coupled PV is not active (passive PV charging during a discharge step
          partially offsets the SoC reduction, making actual_delta smaller than
          planned_delta and producing a spuriously low efficiency reading)

        The correction is the mean of the last 20 ratios (actual/planned), clipped to
        [0.5, 1.05]. It is applied as a multiplier on the DP's discharge-side SoC
        transition so the optimizer plans less discharge within one time step when
        the battery discharges slower than modelled. It does not alter the economic
        cost calculation or degradation model.
        """
        if self._last_result is None:
            return
        if (
            len(self._last_result.mode_schedule) < 1
            or len(self._last_result.soc_schedule_kwh) < 2
        ):
            return

        if self._last_result.mode_schedule[0] != ACTION_DISCHARGING:
            return

        # Only sample when the plan was actually commanded; mode resolution
        # (hybrid → zero_grid, commitment filter, zero_grid/manual control)
        # would otherwise drag the correction towards the 0.5 clip floor.
        if not self._planned_first_step_was_executed(ACTION_DISCHARGING):
            return

        if not self._previous_charge_step_complete():
            return

        # Skip DC-coupled systems: passive PV charging during a discharge step partially
        # offsets SoC reduction, making actual_delta smaller than planned_delta.
        if self.config.get(CONF_PV_DC_COUPLED, False):
            return

        prev_soc = self._last_result.soc_schedule_kwh[0]
        planned_next_soc = self._last_result.soc_schedule_kwh[1]
        planned_delta = prev_soc - planned_next_soc  # positive: SoC goes down

        if planned_delta < 0.1:
            # Too small to measure reliably; skip.
            return

        # Skip if the step crosses the low-SoC discharge derating threshold.
        # The DP plans at the start-SoC limit for the whole step, but the
        # inverter throttles once the threshold is reached mid-step.  The
        # resulting actual/planned ratio reflects the derating, not real
        # efficiency losses, so including it would corrupt the calibration
        # (same guard as the high-SoC check in the charge calibration).
        bc = self.battery_config
        if bc.low_soc_max_discharge_kw > 0:
            threshold_kwh = bc.low_soc_discharge_threshold_pct / 100 * bc.capacity_kwh
            if planned_next_soc < threshold_kwh <= prev_soc:
                _LOGGER.debug(
                    "Discharge efficiency calibration: skipping sample — step crosses "
                    "low-SoC derating threshold (%.1f%% / %.2f kWh)",
                    bc.low_soc_discharge_threshold_pct,
                    threshold_kwh,
                )
                return

        actual_delta = prev_soc - battery_state.soc_kwh  # positive: SoC went down
        # Cap upward to 1.2× planned to filter out unexpected external discharge
        # (e.g. grid outage, SoC sensor noise causing apparent over-discharge).
        actual_delta = max(0.0, min(actual_delta, 1.2 * planned_delta))
        ratio = actual_delta / planned_delta
        # Clip to a physically reasonable range; the correction should never
        # indicate the battery discharged *more* than modelled by more than 5%.
        ratio = max(0.5, min(1.05, ratio))

        self._discharge_eff_samples.append(ratio)
        new_correction = sum(self._discharge_eff_samples) / len(
            self._discharge_eff_samples
        )

        changed = abs(new_correction - self._discharge_eff_correction) > 0.005
        if changed:
            _LOGGER.info(
                "Discharge efficiency correction updated: %.3f → %.3f "
                "(latest ratio=%.3f, n=%d samples, planned Δ=%.2f kWh, actual Δ=%.2f kWh)",
                self._discharge_eff_correction,
                new_correction,
                ratio,
                len(self._discharge_eff_samples),
                planned_delta,
                actual_delta,
            )
        self._discharge_eff_correction = new_correction
        if changed:
            self.hass.async_create_task(self._async_save_discharge_eff_calibration())

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
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="no_price_data",
                retry_after=60,
            )

        # Get price forecast
        if not self._price_sensor:
            _LOGGER.error(
                "OptimizationCoordinator: No price sensor configured. Cannot run optimization."
            )
            self._last_failure_reason = "No price sensor configured"
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="no_price_sensor",
            )

        _LOGGER.debug(
            "OptimizationCoordinator: Fetching price sensor state for %s.",
            self._price_sensor,
        )
        price_state = self.hass.states.get(self._price_sensor)
        price_forecast: list[float] = []
        price_start_times: list[datetime] = []
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
                        translation_domain=DOMAIN,
                        translation_key="price_parse_error",
                        translation_placeholders={
                            "sensor": self._price_sensor,
                            "error": str(e),
                        },
                    ) from e
            else:
                # Sensor unavailable and no model data — create a repair issue
                _LOGGER.error(
                    "OptimizationCoordinator: Price sensor '%s' not available and "
                    "no historical price model data yet.",
                    self._price_sensor,
                )
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    "price_sensor_unavailable",
                    is_fixable=False,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key="price_sensor_unavailable",
                    translation_placeholders={"sensor": self._price_sensor},
                )
                self._last_failure_reason = (
                    f"Price sensor '{self._price_sensor}' not available"
                )
                raise UpdateFailed(
                    translation_domain=DOMAIN,
                    translation_key="price_sensor_unavailable",
                    translation_placeholders={"sensor": self._price_sensor},
                    retry_after=60,
                )

        # Price forecast obtained successfully — clear any prior repair issue
        ir.async_delete_issue(self.hass, DOMAIN, "price_sensor_unavailable")

        # Get feed-in price forecast
        feed_in_is_dynamic = False  # True when feed-in came from a live sensor forecast
        # Native interval of the feed-in forecast; may differ from the grid
        # price sensor (e.g. hourly feed-in vs 15-min grid prices).
        feed_in_interval = price_interval
        feed_in_sensor = self.config.get(CONF_FEED_IN_PRICE_SENSOR)
        if feed_in_sensor:
            feed_in_state = self.hass.states.get(feed_in_sensor)
            if feed_in_state and feed_in_state.state not in ("unknown", "unavailable"):
                feed_in_forecast, feed_in_interval = (
                    extract_price_forecast_with_interval(feed_in_state)
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
        live_entry = self.hass.config_entries.async_get_entry(entry_id)
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
        # Reuse the user-tunable zero-grid deadband as the hysteresis band for
        # the hybrid-mode idle/zero_grid and charging/zero_grid transitions
        # below: both address the same real-time sensor-noise problem the
        # deadband already exists to solve.
        hybrid_deadband_w = float(
            live_options.get(
                CONF_ZERO_GRID_DEADBAND_W,
                self.config.get(
                    CONF_ZERO_GRID_DEADBAND_W, DEFAULT_ZERO_GRID_DEADBAND_W
                ),
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
            # Resample from the feed-in sensor's own interval to the grid price
            # interval. Using price_interval for both would silently misalign
            # the feed-in series whenever the two sensors publish at different
            # resolutions (e.g. hourly feed-in with 15-min grid prices).
            resampled_feed_in = resample_forecast(
                feed_in_forecast, feed_in_interval, price_interval
            )
            if not resampled_feed_in:
                # Downsampling a feed-in series shorter than one grid-price
                # period yields an empty list. An empty feed-in forecast must
                # never reach the optimizer: each step would fall back to the
                # grid buy price and the terminal value of stored energy would
                # become 0, making PV arbitrage look unprofitable. Fall back to
                # the fixed feed-in price (same as an unavailable sensor).
                fixed_price = float(
                    self.config.get(
                        CONF_FIXED_FEED_IN_PRICE, DEFAULT_FIXED_FEED_IN_PRICE
                    )
                )
                resampled_feed_in = [fixed_price] * len(resampled_prices)

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

        # PV curtailment override: zero out all PV when the switch is active.
        # The optimizer then plans charging from the grid instead of relying on
        # phantom solar production.
        if self._pv_curtailed:
            _LOGGER.debug(
                "PV curtailment active: zeroing AC PV (%d steps) and DC PV forecast",
                len(pv_forecast),
            )
            pv_forecast = [0.0] * len(pv_forecast)
            if pv_dc_forecast is not None:
                pv_dc_forecast = [0.0] * len(pv_dc_forecast)

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

        # Charge efficiency calibration: compare previous planned SoC with actual SoC
        # to detect systematic over-estimation of charge efficiency (e.g. CV-phase).
        self._update_charge_eff_calibration(battery_state)
        # Discharge efficiency calibration: same principle for discharging steps.
        self._update_discharge_eff_calibration(battery_state)

        battery_config = self.battery_config

        # Apply charge efficiency correction only to the charge-side SoC
        # transition: when the battery charges slower than modelled, the DP
        # should plan less charge within the step. Economic costs still use the
        # nominal RTE so a charging-speed problem is not double-counted as extra
        # energy cost or degradation.
        nominal_sqrt_rte = math.sqrt(battery_config.round_trip_efficiency)
        charge_eff_override: float | None = None
        if self._charge_eff_correction < 0.995:
            charge_eff_override = nominal_sqrt_rte * self._charge_eff_correction
            _LOGGER.debug(
                "Charge efficiency correction %.3f applied: charge_eff %.4f → %.4f",
                self._charge_eff_correction,
                nominal_sqrt_rte,
                charge_eff_override,
            )

        # Apply discharge efficiency correction only to the discharge-side SoC
        # transition: when the battery discharges slower than modelled, the DP
        # should plan less discharge within the step. Economic costs still use
        # the nominal RTE.
        #
        # The SoC transition is: soc -= power * hours / discharge_eff
        # To reduce planned SoC drop by factor `correction`, we need a LARGER
        # discharge_eff (dividing by a larger value gives a smaller drop).
        # Hence we divide sqrt(RTE) by the correction, not multiply.
        # This may yield discharge_eff_override > 1, which is fine here because
        # the override only affects SoC state transitions, not the economic cost.
        discharge_eff_override: float | None = None
        if self._discharge_eff_correction < 0.995:
            discharge_eff_override = nominal_sqrt_rte / self._discharge_eff_correction
            _LOGGER.debug(
                "Discharge efficiency correction %.3f applied: discharge_eff %.4f → %.4f",
                self._discharge_eff_correction,
                nominal_sqrt_rte,
                discharge_eff_override,
            )

        _LOGGER.debug("OptimizationCoordinator: Calling optimize_battery_schedule.")
        # Run optimization
        _LOGGER.debug(
            "Running optimization: SoC=%.1f%%, %d steps, %d prices",
            battery_state.soc_percent,
            n_steps,
            len(resampled_prices),
        )

        # Convert degradation cost from per-cycle to per-kWh throughput for the optimizer.
        # One full cycle moves the usable capacity (max_soc_kwh - min_soc_kwh) through
        # the battery TWICE: once charging and once discharging. The optimizer applies
        # degradation to throughput in both directions, so divide by 2 × usable_kwh —
        # dividing by usable_kwh alone would make a full cycle cost double the
        # configured per-cycle value.
        usable_kwh = battery_config.max_soc_kwh - battery_config.min_soc_kwh
        degradation_cost_per_kwh = (
            degradation_cost / (2 * usable_kwh) if usable_kwh > 0 else degradation_cost
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
            charge_eff_override,
            discharge_eff_override,
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
            if self._pv_curtailed:
                # PV is unavailable: follow the DP schedule so the battery charges
                # from the grid at negative prices instead of trying to maintain
                # zero-grid by discharging.
                effective_mode = result.optimal_mode
                effective_power = result.optimal_power_kw
            else:
                effective_mode = "zero_grid"
                effective_power = 0.0
        elif self._control_mode == MODE_MANUAL:
            manual_w = self._get_manual_setpoint_w()
            effective_mode = "manual"
            effective_power = manual_w / 1000  # kW for output sensors
        elif self._control_mode in (MODE_HYBRID, MODE_HYBRID_PLUS):
            # Hybrid: DP schedule for arbitrage, zero_grid for self-consumption.
            # Hybrid+ additionally consults the price forecast (via the shadow
            # price) before storing PV surplus, so surplus is exported when the
            # battery can be filled more cheaply later.
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
                # Hysteresis: use a ±hybrid_deadband_w band (the user-tunable
                # zero-grid deadband) around the 0 W crossing so grid power
                # hovering near zero (e.g. consumption ≈ PV production) doesn't
                # flip idle/zero_grid every realtime tick.
                if self._last_hybrid_idle_decision == "zero_grid":
                    # Was capturing surplus; keep doing so until grid climbs
                    # solidly positive (surplus has genuinely disappeared).
                    has_pv_surplus = current_grid < hybrid_deadband_w
                else:
                    # Was idle; only start capturing once surplus is solid.
                    has_pv_surplus = current_grid < -hybrid_deadband_w
                # Hybrid+: check the forecast before capturing surplus. The
                # shadow price λ already prices in upcoming cheap-surplus hours
                # (e.g. the midday PV peak at low prices): when λ × sqrt(RTE)
                # is below the current feed-in price, exporting now is worth
                # more than storing, so surplus capture is blocked.
                capture_blocked = False
                if self._control_mode == MODE_HYBRID_PLUS:
                    current_feed_in = (
                        resampled_feed_in[0]
                        if resampled_feed_in
                        else float(
                            self.config.get(
                                CONF_FIXED_FEED_IN_PRICE, DEFAULT_FIXED_FEED_IN_PRICE
                            )
                        )
                    )
                    capture_blocked = not self._hybrid_plus_should_capture_surplus(
                        result.shadow_price_eur_kwh, current_feed_in
                    )
                self._hybrid_plus_capture_blocked = capture_blocked
                if has_upcoming_discharge and not has_pv_surplus:
                    # Preserve capacity (discharge planned, no PV surplus)
                    effective_mode = ACTION_IDLE
                elif capture_blocked and has_pv_surplus:
                    # Hybrid+: export the surplus at the current feed-in price
                    # instead of storing it; the DP schedule charges later when
                    # surplus is cheaper.
                    effective_mode = ACTION_IDLE
                else:
                    # Either no discharge planned, or PV surplus to capture
                    effective_mode = "zero_grid"
                self._last_hybrid_idle_decision = effective_mode
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
                    self._last_hybrid_charge_decision = ACTION_CHARGING
                else:
                    # Hysteresis: apply a ±5% band around the coverage threshold
                    # (same pattern as the discharge decision above) so PV surplus
                    # hovering near _SURPLUS_COVERS_PLAN_FRACTION of the planned
                    # charge power doesn't flip zero_grid/charging every tick.
                    # • Was zero_grid → stay unless surplus drops below 95% of threshold
                    # • Was charging → switch only once surplus reaches 105% of threshold
                    if self._last_hybrid_charge_decision == "zero_grid":
                        coverage_threshold = _SURPLUS_COVERS_PLAN_FRACTION * 0.95
                    else:
                        coverage_threshold = _SURPLUS_COVERS_PLAN_FRACTION * 1.05
                    if (
                        -current_grid
                        >= result.optimal_power_kw * 1000 * coverage_threshold
                    ):
                        # PV surplus covers (most of) the planned charge: use
                        # zero_grid to dynamically match the actual surplus instead
                        # of fixed-rate charging. Fixed charging may import from
                        # grid when clouds pass.
                        effective_mode = "zero_grid"
                        effective_power = 0.0
                        self._last_hybrid_charge_decision = "zero_grid"
                    else:
                        # The DP planned substantially more charging than the
                        # current export surplus — it wants grid charging (e.g. a
                        # cheap price hour that happens to coincide with a small PV
                        # surplus). Zero_grid would only charge the surplus and
                        # forfeit the planned arbitrage, so follow the schedule.
                        effective_mode = result.optimal_mode
                        effective_power = result.optimal_power_kw
                        self._last_hybrid_charge_decision = ACTION_CHARGING
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
        commitment_locked = False
        commitment_reason = ""
        if self._control_mode not in (MODE_ZERO_GRID, MODE_MANUAL):
            sqrt_rte = battery_config.round_trip_efficiency**0.5
            # Same economics as the post-DP oscillation filter in optimizer.py:
            # min_arbitrage_spread = (2 x degradation + min_spread) / sqrt(RTE).
            commit_spread = (2.0 * degradation_cost_per_kwh + min_spread) / sqrt_rte
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
                    commitment_locked = True
                    commitment_reason = "power_locked"
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
                    commitment_locked = True
                    commitment_reason = "idle_suppressed"
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

        # Append to optimizer run history for diagnostics
        self._optimizer_run_log.append(
            {
                "timestamp": dt_util.now().isoformat(),
                "trigger_source": self._optimization_trigger_source,
                "control_mode": self._control_mode,
                "dp_mode": result.optimal_mode,
                "dp_power_kw": round(result.optimal_power_kw, 3),
                "effective_mode": effective_mode,
                "effective_power_kw": round(effective_power, 3),
                "setpoint_kw": round(control_action["target_power_kw"], 3),
                "raw_target_kw": round(control_action["raw_target_w"] / 1000, 3),
                "soc_kwh": round(battery_state.soc_kwh, 3),
                "soc_percent": round(battery_state.soc_percent, 1),
                "current_price": round(resampled_prices[0], 4)
                if resampled_prices
                else None,
                "current_feed_in_price": round(resampled_feed_in[0], 4)
                if resampled_feed_in
                else None,
                "shadow_price_eur_kwh": round(result.shadow_price_eur_kwh, 4),
                "grid_kw": round(current_grid / 1000, 3),
                "commitment_locked": commitment_locked,
                "commitment_reason": commitment_reason,
                "charge_eff_correction": round(self._charge_eff_correction, 4),
                "charge_eff_samples": len(self._charge_eff_samples),
                "discharge_eff_correction": round(self._discharge_eff_correction, 4),
                "discharge_eff_samples": len(self._discharge_eff_samples),
            }
        )
        self._optimization_trigger_source = "unknown"

        # Split combined setpoint across individual batteries
        combined_setpoint_kw = control_action["target_power_kw"]  # positive=charge
        battery_setpoints = self._split_setpoint(
            combined_setpoint_kw, control_action["mode"]
        )

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
            "charge_eff_correction": round(self._charge_eff_correction, 4),
            "charge_eff_samples": len(self._charge_eff_samples),
            "discharge_eff_correction": round(self._discharge_eff_correction, 4),
            "discharge_eff_samples": len(self._discharge_eff_samples),
            "timestamp": dt_util.utcnow(),
        }

"""Optimization coordinator for the Battery Controller integration."""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
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
from .efficiency_curve import EfficiencyCurve, interpolate_efficiency
from .const import (
    DOMAIN,
    ACTION_CHARGING,
    ACTION_DISCHARGING,
    ACTION_IDLE,
    DC_TO_AC_INVERTER_EFFICIENCY,
    CONF_BATTERY_ENERGY_CHARGED_SENSOR,
    CONF_BATTERY_ENERGY_DISCHARGED_SENSOR,
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
    CONF_MAX_GRID_POWER_KW,
    DEFAULT_MAX_GRID_POWER_KW,
    CONF_PV_DC_COUPLED,
    CONF_PV_DC_PEAK_POWER_KWP,
    CONF_ZERO_GRID_DEADBAND_W,
    DEFAULT_ZERO_GRID_DEADBAND_W,
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
    battery_energy_sensor_ids,
    synthesize_timestamps,
    compute_step_durations_hours,
    extract_price_forecast_with_timestamps,
    get_sensor_value,
    resample_forecast,
    resample_to_steps,
    state_has_value,
    usable_state,
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

# Efficiency calibration tuning.
#
# A sample is only worth taking when the planned SoC change is comfortably
# larger than the resolution of whatever measured it. The energy counters are
# fine-grained, so the floor there is just "big enough to be a real action". A
# SoC sensor reporting whole percent, by contrast, quantises at capacity/100 —
# 0.1 kWh on a 10 kWh pack, which is exactly the old fixed floor, so a single
# sample carried up to +/-50 % quantisation error. On that path the floor
# scales with the observed quantum instead.
_CALIBRATION_MIN_DELTA_KWH = 0.1
_CALIBRATION_SOC_QUANTUM_FACTOR = 4.0

# Samples outside this window are dropped rather than clipped into it. The
# window is wide enough to contain genuine derating and far enough above 1.0
# that ordinary measurement noise is not truncated on one side — the previous
# code capped the ratio at 1.05 while allowing it down to 0.5, so symmetric
# noise biased the mean downward and could apply a correction below 1.0 to a
# perfectly healthy battery. The applied correction is still clamped (below).
_CALIBRATION_ACCEPT_MIN = 0.5
_CALIBRATION_ACCEPT_MAX = 1.5
# Bounds on the correction actually handed to the optimizer.
_CALIBRATION_APPLY_MIN = 0.5
_CALIBRATION_APPLY_MAX = 1.05
# Number of observations averaged into one correction.
_CALIBRATION_WINDOW = 20
# A correction is only persisted (and logged) once it moves by more than this.
_CALIBRATION_SIGNIFICANT_CHANGE = 0.005


@dataclass(frozen=True)
class _CalibrationSpec:
    """What distinguishes the charge- from the discharge-side calibration.

    Both sides answer the same question — did the battery move as much energy
    as the DP planned for the step — so they share one implementation, and only
    the direction-dependent details live here.
    """

    name: str  # "charge" / "discharge", used in log messages
    action: str  # planned first-step mode that makes a sample eligible
    counter_key: str  # key into the cumulative throughput counters
    derate_label: str  # "high-SoC" / "low-SoC", used in log messages
    # Inverter derating within the step invalidates a sample; these read the
    # relevant limit and its SoC threshold off the aggregated battery config.
    derate_limit_kw: Callable[[BatteryConfig], float]
    derate_threshold_pct: Callable[[BatteryConfig], float]


def _mode_from_power_kw(power_kw: float) -> str:
    """Classify a measured battery power as charging, discharging or idle."""
    power_w = power_kw * 1000
    if power_w > BATTERY_MODE_THRESHOLD_W:
        return ACTION_CHARGING
    if power_w < -BATTERY_MODE_THRESHOLD_W:
        return ACTION_DISCHARGING
    return ACTION_IDLE


_CHARGE_CALIBRATION = _CalibrationSpec(
    name="charge",
    action=ACTION_CHARGING,
    counter_key="charged",
    derate_label="high-SoC",
    derate_limit_kw=lambda bc: bc.high_soc_max_charge_kw,
    derate_threshold_pct=lambda bc: bc.high_soc_charge_threshold_pct,
)
_DISCHARGE_CALIBRATION = _CalibrationSpec(
    name="discharge",
    action=ACTION_DISCHARGING,
    counter_key="discharged",
    derate_label="low-SoC",
    derate_limit_kw=lambda bc: bc.low_soc_max_discharge_kw,
    derate_threshold_pct=lambda bc: bc.low_soc_discharge_threshold_pct,
)


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
        self._apply_entry_level_config()

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

        # Hysteresis state for the realtime idle→zero_grid upgrade in
        # _resolve_controller_mode. The 15-min run damps its own idle/zero_grid
        # decision (see _last_hybrid_idle_decision), but the realtime loop
        # re-derives the controller mode from the live grid reading on every
        # tick, so it needs its own band and memory or a surplus appearing
        # mid-period flips idle/zero_grid until the next run.
        self._last_idle_upgrade_decision: str = ACTION_IDLE

        # Hysteresis state for the hybrid charging branch: tracks whether we were
        # following the DP schedule ("charging") or capturing PV surplus only
        # ("zero_grid") so surplus hovering near the coverage threshold doesn't
        # flip the mode every realtime-update tick.
        self._last_hybrid_charge_decision: str = ACTION_CHARGING

        # Hysteresis state for the surplus-capture decision: tracks whether the
        # last decision was to store PV surplus ("zero_grid") or export it
        # ("idle") so a shadow price hovering near the feed-in price doesn't
        # flip the mode every optimization run.
        self._last_surplus_capture_decision: str = "zero_grid"

        # Whether the last optimizer run found PV-surplus capture uneconomical
        # (exporting at the current feed-in price is worth more than storing).
        # Gates the realtime idle→zero_grid upgrade in _resolve_controller_mode
        # so a surplus appearing between optimizer runs isn't captured anyway.
        self._surplus_capture_blocked: bool = False

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
        self._charge_eff_samples: deque[float] = deque(maxlen=_CALIBRATION_WINDOW)
        self._charge_eff_correction: float = 1.0

        # Cumulative throughput counters as they stood when _last_result was
        # planned. The difference against the next read is the energy the
        # battery actually moved over that step — a far finer measurement than
        # the SoC delta, which on a whole-percent sensor quantises at
        # capacity/100 (0.1 kWh on a 10 kWh pack).
        self._energy_counter_snapshot: dict[str, float | None] = {
            "charged": None,
            "discharged": None,
        }

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
        self._discharge_eff_samples: deque[float] = deque(maxlen=_CALIBRATION_WINDOW)
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
        self._last_surplus_capture_decision = "zero_grid"
        self._surplus_capture_blocked = False
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
        """Enable or disable PV curtailment mode.

        Propagated to the forecast coordinator: while production is
        deliberately suppressed, the shortfall against the PV forecast is not a
        forecast error and must not be learned as a per-array correction.
        """
        self._pv_curtailed = value
        self.forecast_coordinator.pv_curtailed = value

    @property
    def charge_eff_correction(self) -> float:
        """Learned multiplier on the charge-side SoC transition (1.0 = nominal).

        Already published in ``data`` and in diagnostics; exposed here so the
        current value can be read without a completed run, the same way
        control_mode and optimization_enabled are.
        """
        return self._charge_eff_correction

    @property
    def charge_eff_sample_count(self) -> int:
        """Number of observations behind charge_eff_correction."""
        return len(self._charge_eff_samples)

    @property
    def discharge_eff_correction(self) -> float:
        """Learned multiplier on the discharge-side SoC transition."""
        return self._discharge_eff_correction

    @property
    def discharge_eff_sample_count(self) -> int:
        """Number of observations behind discharge_eff_correction."""
        return len(self._discharge_eff_samples)

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
            # Single source: create_zero_grid_controller already parsed this
            # from the config, so re-reading it here could drift.
            interval_s = self.zero_grid_controller.config.response_time_s
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
        was_unavailable = self._last_price is None or not state_has_value(old_state)

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
            self._last_feed_in_period_start is None or not state_has_value(old_state)
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
        if not state_has_value(old_state) and state_has_value(new_state):
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
        stale_limit_s = (
            STALE_SENSOR_MULTIPLIER * self.zero_grid_controller.config.response_time_s
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
            control_action["target_power_kw"], self._control_mode
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

    def _live_option(self, key: str, default: float) -> float:
        """Read a runtime-tunable option from the live config entry.

        Number entities write straight into entry.options, so reading them from
        the live entry (rather than the snapshot taken at setup) is what lets
        them take effect without an integration reload. Falls back to the setup
        snapshot, then to the default.
        """
        entry = self.hass.config_entries.async_get_entry(
            self.config.get("entry_id", "")
        )
        options = entry.options if entry is not None else {}
        return float(options.get(key, self.config.get(key, default)))

    def _get_manual_setpoint_w(self) -> float:
        """Read the live manual power setpoint from entry options.

        The number entity uses the sensor convention (positive = discharge,
        negative = charge). We negate here to match the internal controller
        convention (positive = charge, negative = discharge).
        """
        # Negate: user enters positive=discharge, controller expects positive=charge
        return -self._live_option(
            CONF_MANUAL_POWER_SETPOINT_W, DEFAULT_MANUAL_POWER_SETPOINT_W
        )

    def _fixed_feed_in_price(self) -> float:
        """Configured fallback feed-in price, in EUR/kWh."""
        return float(
            self.config.get(CONF_FIXED_FEED_IN_PRICE, DEFAULT_FIXED_FEED_IN_PRICE)
        )

    def _current_feed_in_price(self, feed_in_forecast: list[float] | None) -> float:
        """Feed-in price for the step being executed.

        Never None: an empty forecast falls back to the configured fixed price,
        because a missing feed-in price makes the optimizer treat exports at the
        grid buy price and PV arbitrage stops looking profitable.
        """
        if feed_in_forecast:
            return feed_in_forecast[0]
        return self._fixed_feed_in_price()

    def _charge_efficiency_at(self, power_kw: float) -> float:
        """Charge efficiency for a specific AC power, from the curve.

        The DP values every action at its curve efficiency, so the thresholds
        that arbitrate between the DP plan and zero-grid must use the same
        number. The representative scalar (``charge_efficiency``) is a mean over
        5..95 % of nominal power and sits well below the curve at the powers a
        real decision is taken at, which biases those comparisons against the
        DP's own decision. It is only the fallback for a power of zero, where
        the curve's idle-loss point says nothing useful.
        """
        power_kw = abs(power_kw)
        if power_kw <= 0:
            return float(self.battery_config.charge_efficiency)
        return interpolate_efficiency(
            self.battery_config.charge_efficiency_curve_parsed, power_kw
        )

    def _discharge_efficiency_at(self, power_kw: float) -> float:
        """Discharge efficiency for a specific AC power, from the curve.

        Mirrors _charge_efficiency_at for the discharge direction.
        """
        power_kw = abs(power_kw)
        if power_kw <= 0:
            return float(self.battery_config.discharge_efficiency)
        return interpolate_efficiency(
            self.battery_config.discharge_efficiency_curve_parsed, power_kw
        )

    def _should_capture_surplus(
        self,
        shadow_price_eur_kwh: float,
        current_feed_in: float,
        surplus_kw: float,
    ) -> bool:
        """Return whether PV surplus is worth storing rather than exporting.

        Storing 1 kWh of AC surplus puts ``charge_eff`` kWh in the battery, each
        worth the DP shadow price λ — the marginal value of stored energy given
        the full price and PV forecast. Exporting the same kWh yields the
        current feed-in price. When λ × charge_eff is below the feed-in price,
        the forecast says the battery can be filled more cheaply later (e.g.
        the midday PV peak at low prices), so exporting now is worth more than
        storing.

        Applies a ±5% hysteresis band around the feed-in threshold (same
        pattern as the hybrid discharge decision) so a shadow price hovering
        near the feed-in price doesn't flip the decision every run.

        Args:
            shadow_price_eur_kwh: DP shadow price λ (value per stored kWh).
            current_feed_in: Feed-in price for the step being executed.
            surplus_kw: AC surplus that would be stored, in kW. Prices the
                charge at its curve efficiency instead of a nominal scalar.
        """
        if current_feed_in <= 0:
            # Exporting earns nothing (or costs money): always capture.
            self._last_surplus_capture_decision = "zero_grid"
            return True
        store_value = shadow_price_eur_kwh * self._charge_efficiency_at(surplus_kw)
        if self._last_surplus_capture_decision == "zero_grid":
            should_capture = bool(store_value >= current_feed_in * 0.95)
        else:
            should_capture = bool(store_value >= current_feed_in * 1.05)
        self._last_surplus_capture_decision = (
            "zero_grid" if should_capture else ACTION_IDLE
        )
        return should_capture

    def _hybrid_deadband_w(self) -> float:
        """Return the hysteresis band for the hybrid mode transitions, in W.

        Reuses the user-tunable zero-grid deadband: the idle/zero_grid and
        charging/zero_grid transitions address the same real-time sensor-noise
        problem the deadband already exists to solve. Read from the live entry
        options so the number entity takes effect without a reload.
        """
        return self._live_option(
            CONF_ZERO_GRID_DEADBAND_W, DEFAULT_ZERO_GRID_DEADBAND_W
        )

    def _resolve_controller_mode(
        self, effective_mode: str, current_grid_w: float
    ) -> str:
        """Map effective mode to zero_grid_controller mode.

        For idle mode with PV surplus, upgrades to zero_grid when real-time
        power sensors are available. The decision is damped with a band of
        ±zero_grid_deadband_w (the user-tunable number entity, default 50 W)
        around the 0 W crossing, remembered across calls: enter zero_grid only
        once the grid exports more than the band, then stay there until it
        climbs solidly positive.

        Without that band the battery absorbing PV drives the grid to ~0 W,
        which reads as "no surplus", which stops the charge, which sends the
        grid negative again — an oscillation at tick rate. The setpoint
        deadband does not damp it, because the swing between the zero_grid
        setpoint and idle's 0 W is far larger than the deadband.

        The upgrade is suppressed while the last optimizer run found surplus
        capture uneconomical (exporting is worth more than storing per the
        shadow price), so idle genuinely means "export the surplus" — otherwise
        the realtime loop would re-capture the surplus the run just refused.

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
        # follow_schedule or manual, where idle must mean truly stop), and only
        # while the last optimizer run found surplus capture economical.
        if effective_mode == ACTION_IDLE:
            upgrade_allowed = (
                self._control_mode not in (MODE_FOLLOW_SCHEDULE, MODE_MANUAL)
                and not self._surplus_capture_blocked
                and has_power_sensors
            )
            if not upgrade_allowed:
                # Reset the memory so a later upgrade starts from the strict
                # entry threshold rather than the sticky one.
                self._last_idle_upgrade_decision = ACTION_IDLE
                return ACTION_IDLE
            band = self._hybrid_deadband_w()
            if self._last_idle_upgrade_decision == "zero_grid":
                # Was capturing surplus; keep doing so until the grid climbs
                # solidly positive (the surplus has genuinely disappeared).
                has_pv_surplus = current_grid_w < band
            else:
                # Was idle; only start capturing once the surplus is solid, so
                # near-zero import noise does not trigger it.
                has_pv_surplus = current_grid_w < -band
            self._last_idle_upgrade_decision = (
                "zero_grid" if has_pv_surplus else ACTION_IDLE
            )
            return "zero_grid" if has_pv_surplus else ACTION_IDLE
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
            state = usable_state(self.hass, sensor_id)
            if state is None:
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
        state = usable_state(self.hass, sensor_id)
        if state is None:
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
        for attr in (
            "_unsub_price",
            "_unsub_feed_in_price",
            "_unsub_soc",
            "_unsub_forecast",
            "_unsub_mid_period_timer",
            "_unsub_price_model_refresh",
            "_unsub_realtime",
        ):
            unsub = getattr(self, attr)
            if unsub:
                unsub()
                setattr(self, attr, None)
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
            state = usable_state(self.hass, soc_sensor)
            if state is not None:
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

        return BatteryState(
            soc_kwh=soc_kwh,
            soc_percent=soc_percent,
            power_kw=power_kw,
            mode=_mode_from_power_kw(power_kw),
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
        return BatteryState(
            soc_kwh=total_soc_kwh,
            soc_percent=combined_soc_percent,
            power_kw=total_power_kw,
            mode=_mode_from_power_kw(total_power_kw),
        )

    def _split_setpoint(self, total_kw: float, mode: str = "") -> dict[str, float]:
        """Split combined setpoint (kW, positive=charge) to per-battery setpoints.

        ``mode`` is the user-selected CONTROL mode (``self._control_mode``), not
        the resolved zero-grid-controller mode. The controller only ever reports
        ``zero_grid`` / ``follow_schedule`` / ``idle`` / ``manual``, so passing
        that made the hybrid branch below unreachable and put hybrid runs on the
        directional (charge/discharge) selection criterion, which switches
        inverters whenever the schedule reverses direction.

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

        def _hand_to_others(amount_kw: float) -> dict[str, float]:
            """Spread ``amount_kw`` over the non-winning batteries."""
            if others:
                result.update(self._proportional_split(amount_kw, others))
            return result

        if total_kw > 0:
            if winner_cfg.max_soc_kwh - winner_soc_kwh <= 0:
                # Winner is full (can happen via selection hysteresis): hand
                # the whole setpoint to the remaining batteries instead of
                # dropping it.
                return _hand_to_others(total_kw)
            clamped = min(total_kw, winner_cfg.max_charge_at_soc(winner_soc_kwh))
        else:
            if winner_soc_kwh - winner_cfg.min_soc_kwh <= 0:
                # Winner is empty: redistribute the discharge to the others.
                return _hand_to_others(total_kw)
            clamped = max(total_kw, -winner_cfg.max_discharge_at_soc(winner_soc_kwh))

        result[winner] = clamped
        overflow = total_kw - clamped
        if abs(overflow) > 1e-6:
            _hand_to_others(overflow)
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
        self._apply_entry_level_config()
        # The zero-grid controller clamps every real-time setpoint with these
        # limits. It holds its own reference, so rebinding self.battery_config
        # above would otherwise leave it clamping on the configuration captured
        # at integration setup — SoC limits and power ratings changed in a
        # battery subentry would reach the DP but not the ~5 s control loop.
        self.zero_grid_controller.battery_config = self.battery_config

    def _apply_entry_level_config(self) -> None:
        """Overlay entry-level settings onto the aggregated battery config.

        Two groups of settings live on the config entry rather than on a
        battery subentry, so ``BatteryConfig.from_subentry`` (the only factory
        the coordinator uses) can never populate them:

        - **DC coupling** is configured on the PV-array subentries. Without
          this overlay the optimizer always sees ``pv_dc_coupled=False`` and
          never models passive DC MPPT charging.
        - **The grid capacity cap** is a property of the house connection, not
          of any single battery. ``aggregate_battery_configs`` treats an
          unset per-battery cap as "unlimited", so without this overlay the
          configured cap silently never reaches ``calculate_step_cost`` and the
          optimizer plans (and the baseline credits) grid flows beyond the
          physical connection.
        """
        self.battery_config.max_grid_power_kw = float(
            self.config.get(CONF_MAX_GRID_POWER_KW, DEFAULT_MAX_GRID_POWER_KW)
        )
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

    @staticmethod
    async def _async_load_eff_calibration(
        store: storage.Store[dict[str, Any]], name: str
    ) -> tuple[deque[float], float] | None:
        """Read one direction's persisted calibration, or None when absent."""
        stored = await store.async_load()
        if stored is None:
            return None
        samples: deque[float] = deque(
            stored.get("samples", []), maxlen=_CALIBRATION_WINDOW
        )
        correction = float(stored.get("correction", 1.0))
        if correction < 0.995:
            _LOGGER.info(
                "Restored %s efficiency calibration: correction=%.3f, n=%d samples",
                name,
                correction,
                len(samples),
            )
        return samples, correction

    @staticmethod
    def _log_eff_calibration_reset(
        name: str, samples: deque[float], correction: float
    ) -> None:
        """Log a calibration reset, but only when there was something to clear."""
        if samples or abs(correction - 1.0) > 1e-9:
            _LOGGER.info(
                "Resetting %s efficiency calibration: %.3f (%d samples) -> 1.000",
                name,
                correction,
                len(samples),
            )

    async def _async_load_charge_eff_calibration(self) -> None:
        """Load persisted charge efficiency calibration from storage."""
        loaded = await self._async_load_eff_calibration(
            self._charge_eff_store, "charge"
        )
        if loaded is not None:
            self._charge_eff_samples, self._charge_eff_correction = loaded

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
        self._log_eff_calibration_reset(
            "charge", self._charge_eff_samples, self._charge_eff_correction
        )
        self._charge_eff_samples.clear()
        self._charge_eff_correction = 1.0
        await self._async_save_charge_eff_calibration()

    def _read_energy_total_kwh(self, conf_key: str) -> float | None:
        """Sum the configured cumulative energy counters, in kWh.

        Returns None when no counter is configured or any of them is
        unavailable: a partial sum would look like a jump backwards on the next
        read and poison the delta.
        """
        sensor_ids = battery_energy_sensor_ids(self._battery_subentries, conf_key)
        if not sensor_ids:
            return None
        total = 0.0
        for sensor_id in sensor_ids:
            state = usable_state(self.hass, sensor_id)
            if state is None:
                return None
            try:
                value = float(state.state)
            except (ValueError, TypeError):
                return None
            unit = str(state.attributes.get("unit_of_measurement") or "kWh")
            if unit == "Wh":
                value /= 1000.0
            elif unit == "MWh":
                value *= 1000.0
            elif unit != "kWh":
                self._warn_unit_once(sensor_id, unit, "treating value as kWh")
            total += value
        return total

    def _read_energy_totals(self) -> dict[str, float | None]:
        """Read both cumulative throughput counters at one instant."""
        return {
            "charged": self._read_energy_total_kwh(CONF_BATTERY_ENERGY_CHARGED_SENSOR),
            "discharged": self._read_energy_total_kwh(
                CONF_BATTERY_ENERGY_DISCHARGED_SENSOR
            ),
        }

    def _counter_delta_kwh(
        self, key: str, totals_now: dict[str, float | None]
    ) -> float | None:
        """Throughput measured by the counters since the plan was made.

        None when either end of the interval is missing, or when the counter
        went backwards — a total_increasing sensor that was reset or replaced,
        where the difference is meaningless rather than negative.
        """
        before = self._energy_counter_snapshot.get(key)
        after = totals_now.get(key)
        if before is None or after is None:
            return None
        delta = after - before
        if delta < 0:
            _LOGGER.debug(
                "Efficiency calibration: %s counter went backwards "
                "(%.3f -> %.3f kWh); treating as a meter reset",
                key,
                before,
                after,
            )
            return None
        return delta

    def _soc_quantum_kwh(self) -> float:
        """Estimate the resolution of the aggregated SoC measurement, in kWh.

        Derived from the decimals each SoC sensor actually reports: a state of
        "47" is whole-percent, "47.3" is ten times finer. Summed across packs
        because the aggregate SoC is a sum and the individual quantisation
        errors can align. Returns 0.0 when nothing can be determined, which
        leaves the caller on its fixed floor.
        """
        total = 0.0
        for (_sid, cfg), (_sid2, data) in zip(
            self._individual_battery_configs, self._battery_subentries
        ):
            sensor_id = data.get(CONF_BATTERY_SOC_SENSOR)
            if not sensor_id:
                continue
            state = usable_state(self.hass, sensor_id)
            if state is None:
                continue
            raw = state.state.strip()
            decimals = len(raw.split(".", 1)[1]) if "." in raw else 0
            step = 10.0**-decimals
            unit = str(state.attributes.get("unit_of_measurement") or "%")
            if unit == "kWh":
                total += step
            elif unit == "Wh":
                total += step / 1000.0
            else:  # percent (or unitless, which is treated as percent elsewhere)
                total += step / 100.0 * cfg.capacity_kwh
        return total

    def _min_planned_delta_kwh(self, from_counters: bool) -> float:
        """Smallest planned SoC change worth sampling, given how it is measured."""
        if from_counters:
            return _CALIBRATION_MIN_DELTA_KWH
        quantum = self._soc_quantum_kwh()
        return max(
            _CALIBRATION_MIN_DELTA_KWH,
            _CALIBRATION_SOC_QUANTUM_FACTOR * quantum,
        )

    def _record_calibration_sample(
        self,
        *,
        direction: str,
        samples: deque[float],
        current_correction: float,
        planned_delta: float,
        actual_delta: float,
        source: str,
    ) -> float:
        """Fold one actual/planned observation into a correction factor.

        Samples outside the acceptance window are dropped rather than clipped
        into it. Clipping was not symmetric around 1.0 — the ratio was capped
        at 1.05 but allowed down to 0.5 — so ordinary measurement noise pulled
        the mean below 1.0 and a healthy battery could end up with a correction
        applied to it. Only the resulting mean is clamped, and only for use.
        """
        ratio = actual_delta / planned_delta
        if not (_CALIBRATION_ACCEPT_MIN <= ratio <= _CALIBRATION_ACCEPT_MAX):
            _LOGGER.debug(
                "%s efficiency calibration: dropping implausible sample "
                "(ratio=%.3f from %s, planned Δ=%.2f kWh, actual Δ=%.2f kWh)",
                direction.capitalize(),
                ratio,
                source,
                planned_delta,
                actual_delta,
            )
            return current_correction

        samples.append(ratio)
        mean = sum(samples) / len(samples)
        new_correction = max(_CALIBRATION_APPLY_MIN, min(_CALIBRATION_APPLY_MAX, mean))
        if abs(new_correction - current_correction) > _CALIBRATION_SIGNIFICANT_CHANGE:
            _LOGGER.info(
                "%s efficiency correction updated: %.3f → %.3f "
                "(latest ratio=%.3f from %s, n=%d samples, "
                "planned Δ=%.2f kWh, actual Δ=%.2f kWh)",
                direction.capitalize(),
                current_correction,
                new_correction,
                ratio,
                source,
                len(samples),
                planned_delta,
                actual_delta,
            )
        return new_correction

    def _previous_step_complete(self) -> bool:
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

    def _update_eff_calibration(
        self,
        spec: _CalibrationSpec,
        battery_state: BatteryState,
        samples: deque[float],
        correction: float,
        energy_totals_now: dict[str, float | None] | None,
    ) -> float:
        """Score the previous plan against reality and return the new correction.

        Both directions ask the same question: did the battery move as much
        energy within the step as the DP assumed? A sample is only taken when:
        - The previous optimizer step planned this direction actively
        - The planned step was actually commanded unchanged (no hybrid/zero_grid
          override, no commitment-filter power lock)
        - The full planned step has elapsed
        - The planned delta is large enough to be measurable at the resolution
          of whatever measured it (energy counter or SoC sensor)
        - DC-coupled PV is not active: passive PV charging inflates the measured
          charge delta and offsets the measured discharge delta
        - The step does not cross the inverter's derating threshold. The DP
          plans full power for the whole step while the inverter throttles once
          the threshold is passed mid-step, so the ratio would reflect the
          derating rather than an efficiency loss.

        The correction is the mean of the last _CALIBRATION_WINDOW ratios
        (actual/planned), clamped for use. It is applied as a multiplier on the
        DP's SoC transition for this direction only; the economic cost model is
        untouched. Returns ``correction`` unchanged when no sample is taken.
        """
        result = self._last_result
        if result is None:
            return correction
        if len(result.mode_schedule) < 1 or len(result.soc_schedule_kwh) < 2:
            return correction
        if result.mode_schedule[0] != spec.action:
            return correction

        # Only sample when the plan was actually commanded; mode resolution
        # (hybrid → zero_grid, commitment filter, zero_grid/manual control)
        # would otherwise drag the correction towards the 0.5 clip floor.
        if not self._planned_first_step_was_executed(spec.action):
            return correction
        if not self._previous_step_complete():
            return correction
        if self.config.get(CONF_PV_DC_COUPLED, False):
            return correction

        prev_soc = result.soc_schedule_kwh[0]
        planned_next_soc = result.soc_schedule_kwh[1]
        # Positive in both directions: the magnitude of the planned SoC change.
        planned_delta = (
            planned_next_soc - prev_soc
            if spec.action == ACTION_CHARGING
            else prev_soc - planned_next_soc
        )

        # Prefer the cumulative throughput counter over the SoC delta. Both
        # measure the same quantity — energy moved through the battery, which
        # is what the planned SoC change represents — but the counter is not
        # limited by the SoC sensor's step size.
        counter_delta = (
            self._counter_delta_kwh(spec.counter_key, energy_totals_now)
            if energy_totals_now is not None
            else None
        )
        if planned_delta < self._min_planned_delta_kwh(counter_delta is not None):
            # Too small to measure reliably at this resolution; skip.
            return correction

        bc = self.battery_config
        if spec.derate_limit_kw(bc) > 0:
            threshold_kwh = spec.derate_threshold_pct(bc) / 100 * bc.capacity_kwh
            if (
                min(prev_soc, planned_next_soc)
                < threshold_kwh
                <= max(prev_soc, planned_next_soc)
            ):
                _LOGGER.debug(
                    "%s efficiency calibration: skipping sample — step crosses "
                    "%s derating threshold (%.1f%% / %.2f kWh)",
                    spec.name.capitalize(),
                    spec.derate_label,
                    spec.derate_threshold_pct(bc),
                    threshold_kwh,
                )
                return correction

        if counter_delta is not None:
            actual_delta = counter_delta
            source = "energy counter"
        else:
            measured = (
                battery_state.soc_kwh - prev_soc
                if spec.action == ACTION_CHARGING
                else prev_soc - battery_state.soc_kwh
            )
            actual_delta = max(0.0, measured)
            source = "SoC delta"

        return self._record_calibration_sample(
            direction=spec.name,
            samples=samples,
            current_correction=correction,
            planned_delta=planned_delta,
            actual_delta=actual_delta,
            source=source,
        )

    def _update_charge_eff_calibration(
        self,
        battery_state: BatteryState,
        energy_totals_now: dict[str, float | None] | None = None,
    ) -> None:
        """Fold the previous charging step's outcome into the charge correction."""
        previous = self._charge_eff_correction
        self._charge_eff_correction = self._update_eff_calibration(
            _CHARGE_CALIBRATION,
            battery_state,
            self._charge_eff_samples,
            previous,
            energy_totals_now,
        )
        if (
            abs(self._charge_eff_correction - previous)
            > _CALIBRATION_SIGNIFICANT_CHANGE
        ):
            self.hass.async_create_task(self._async_save_charge_eff_calibration())

    async def _async_load_discharge_eff_calibration(self) -> None:
        """Load persisted discharge efficiency calibration from storage."""
        loaded = await self._async_load_eff_calibration(
            self._discharge_eff_store, "discharge"
        )
        if loaded is not None:
            self._discharge_eff_samples, self._discharge_eff_correction = loaded

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
        self._log_eff_calibration_reset(
            "discharge", self._discharge_eff_samples, self._discharge_eff_correction
        )
        self._discharge_eff_samples.clear()
        self._discharge_eff_correction = 1.0
        await self._async_save_discharge_eff_calibration()

    def _update_discharge_eff_calibration(
        self,
        battery_state: BatteryState,
        energy_totals_now: dict[str, float | None] | None = None,
    ) -> None:
        """Fold the previous discharging step's outcome into the discharge correction."""
        previous = self._discharge_eff_correction
        self._discharge_eff_correction = self._update_eff_calibration(
            _DISCHARGE_CALIBRATION,
            battery_state,
            self._discharge_eff_samples,
            previous,
            energy_totals_now,
        )
        if (
            abs(self._discharge_eff_correction - previous)
            > _CALIBRATION_SIGNIFICANT_CHANGE
        ):
            self.hass.async_create_task(self._async_save_discharge_eff_calibration())

    def _model_extension(
        self,
        model: PriceForecastModel,
        steps_covered: int,
        steps_needed: int,
        interval_minutes: int,
    ) -> list[float]:
        """Extend a price series past the live forecast with a historical model.

        ``steps_covered`` steps are already covered, so the model is asked for
        the hours after them and the weather series are sliced to the same
        offset — the model's GHI/wind features must line up with the hours it
        is predicting.
        """
        hours_already = int(steps_covered * interval_minutes / 60)
        hours_for_model = (steps_needed * interval_minutes + 59) // 60  # ceiling
        extension_start = dt_util.now().replace(
            minute=0, second=0, microsecond=0
        ) + timedelta(hours=hours_already)
        weather = self.weather_coordinator.data or {}
        ghi = weather.get("radiation_forecast", [])
        wind = weather.get("wind_speed_forecast", [])
        raw = model.forecast(
            hours=hours_for_model,
            start_time=extension_start,
            ghi_forecast=ghi[hours_already:] if ghi else None,
            wind_forecast=wind[hours_already:] if wind else None,
        )
        return resample_forecast(raw, 60, interval_minutes)[:steps_needed]

    def _model_reference_series(
        self, model: PriceForecastModel, n_steps: int, interval_minutes: int
    ) -> list[float]:
        """What the historical model predicts for the horizon being optimized.

        Published alongside the live prices so users can compare prediction
        against actual; it never feeds the optimizer.
        """
        weather = self.weather_coordinator.data or {}
        total_hours = (n_steps * interval_minutes + 59) // 60
        raw = model.forecast(
            hours=total_hours,
            ghi_forecast=weather.get("radiation_forecast"),
            wind_forecast=weather.get("wind_speed_forecast"),
        )
        return resample_forecast(raw, 60, interval_minutes)[:n_steps]

    def _resolve_effective_mode(
        self,
        result: OptimizationResult,
        current_grid: float,
        resampled_feed_in: list[float] | None,
        hybrid_deadband_w: float,
    ) -> tuple[str, float]:
        """Turn the DP schedule into what the controller should actually do.

        follow_schedule executes the plan verbatim; zero_grid and manual ignore
        it; hybrid and hybrid+ arbitrate between the plan and surplus-following
        per step. Every branch is damped with hysteresis so a value hovering at
        a threshold does not flip the mode on every run.

        Returns:
            (effective_mode, effective_power_kw)
        """
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
            # Default to allowing capture; the idle and discharge branches
            # overrule it so a block never outlives the run that decided it.
            self._surplus_capture_blocked = False
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
                    current_feed_in = self._current_feed_in_price(resampled_feed_in)
                    capture_blocked = not self._should_capture_surplus(
                        result.shadow_price_eur_kwh,
                        current_feed_in,
                        max(0.0, -current_grid) / 1000.0,
                    )
                self._surplus_capture_blocked = capture_blocked
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
                # Use shadow price as the threshold: net sell value per kWh
                # stored = feed_in × discharge_eff at the planned power — the
                # same efficiency the DP priced this action with. If that
                # exceeds the shadow price (the value of keeping the energy for
                # future use), exporting is better.
                #
                # Hysteresis (P3.1): apply a ±5% band around the threshold to prevent
                # oscillation when shadow_price ≈ net_sell_value.
                # • Was discharging → continue unless net_sell_value < threshold × 0.95
                # • Was not discharging → start only if net_sell_value ≥ threshold × 1.05
                current_feed_in = self._current_feed_in_price(resampled_feed_in)
                net_sell_value = current_feed_in * self._discharge_efficiency_at(
                    result.optimal_power_kw
                )
                threshold = result.shadow_price_eur_kwh
                if self._last_hybrid_decision == ACTION_DISCHARGING:
                    should_discharge = net_sell_value >= threshold * 0.95
                else:
                    should_discharge = net_sell_value >= threshold * 1.05

                if should_discharge:
                    effective_mode = ACTION_DISCHARGING
                    effective_power = result.optimal_power_kw
                    self._last_hybrid_decision = ACTION_DISCHARGING
                    self._surplus_capture_blocked = False
                else:
                    # Shadow price > sell value: energy is more valuable later.
                    # That is a decision to *hold*, so falling back to zero_grid
                    # unconditionally would be a contradiction: zero_grid buys
                    # PV surplus that would otherwise be exported, at the cost
                    # of the feed-in price the veto just refused to sell at.
                    # Only capture the surplus when storing it actually beats
                    # exporting it (λ × charge_eff ≥ feed_in); otherwise hold
                    # and let the surplus go to the grid. Unlike the idle
                    # branch, this test runs in plain hybrid too — hybrid's
                    # unconditional surplus capture applies when the DP has no
                    # opinion, not when it is overruling an explicit plan.
                    capture = self._should_capture_surplus(
                        result.shadow_price_eur_kwh,
                        current_feed_in,
                        max(0.0, -current_grid) / 1000.0,
                    )
                    effective_mode = "zero_grid" if capture else ACTION_IDLE
                    effective_power = 0.0
                    self._last_hybrid_decision = effective_mode
                    self._surplus_capture_blocked = not capture
            elif result.optimal_mode == ACTION_CHARGING and current_grid < 0:
                current_feed_in = self._current_feed_in_price(resampled_feed_in)
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

        return effective_mode, effective_power

    def _apply_commitment_filter(
        self,
        effective_mode: str,
        effective_power: float,
        *,
        battery_state: BatteryState,
        battery_config: BatteryConfig,
        current_price: float,
        current_step_start: datetime | None,
        degradation_cost_per_kwh: float,
        min_spread: float,
    ) -> tuple[str, float, bool, str]:
        """Hold an active charge/discharge unless the economics really changed.

        Re-optimizing every 15 minutes within one price period would otherwise
        produce erratic setpoints; the commitment is only released when the
        price moves past the same spread the post-DP oscillation filter uses,
        when the SoC hits a limit, or when the plan reverses direction.

        Returns:
            (effective_mode, effective_power_kw, commitment_locked, reason)
        """
        commitment_locked = False
        commitment_reason = ""
        if self._control_mode not in (MODE_ZERO_GRID, MODE_MANUAL):
            sqrt_rte = battery_config.round_trip_efficiency**0.5
            # Same economics as the post-DP oscillation filter in optimizer.py:
            # min_arbitrage_spread = (2 x degradation + min_spread) / sqrt(RTE).
            commit_spread = (2.0 * degradation_cost_per_kwh + min_spread) / sqrt_rte
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

        return effective_mode, effective_power, commitment_locked, commitment_reason

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
        # Anchor of feed_in_forecast[0]. A fixed price has no timeline of its
        # own, so it inherits the run time and lines up with step 0 by
        # construction; a sensor forecast carries its own period starts.
        feed_in_start: datetime = dt_util.utcnow()
        feed_in_sensor = self.config.get(CONF_FEED_IN_PRICE_SENSOR)
        if feed_in_sensor:
            feed_in_state = usable_state(self.hass, feed_in_sensor)
            if feed_in_state is not None:
                feed_in_forecast, feed_in_starts, feed_in_interval = (
                    extract_price_forecast_with_timestamps(feed_in_state)
                )
                if feed_in_starts:
                    feed_in_start = feed_in_starts[0]
                feed_in_is_dynamic = True
            else:
                # Sensor unavailable - fall back to fixed price
                feed_in_forecast = [self._fixed_feed_in_price()] * len(price_forecast)
        else:
            # Use fixed feed-in price
            feed_in_forecast = [self._fixed_feed_in_price()] * len(price_forecast)

        # Get optimization parameters — read runtime-tunable values from live options
        degradation_cost = self._live_option(
            CONF_DEGRADATION_COST_PER_CYCLE, DEFAULT_DEGRADATION_COST_PER_CYCLE
        )
        min_spread = self._live_option(CONF_MIN_PRICE_SPREAD, DEFAULT_MIN_PRICE_SPREAD)
        hybrid_deadband_w = self._hybrid_deadband_w()

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
            original_steps = len(resampled_prices)
            resampled_prices = resampled_prices + self._model_extension(
                self._price_model, original_steps, steps_needed, price_interval
            )
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
            price_forecast_model = self._model_reference_series(
                self._price_model, len(resampled_prices), price_interval
            )

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

        # Absolute step windows, used to project every other series onto the
        # DP's own time grid. Step 0 runs from now to the next price boundary;
        # each later step spans one full price period.
        step_starts: list[datetime] = [now_utc] + list(
            price_start_times[1 : len(resampled_prices)]
        )
        while len(step_starts) < len(resampled_prices):
            step_starts.append(
                step_starts[-1] + timedelta(minutes=price_interval)
                if step_starts
                else now_utc
            )

        resampled_feed_in = None
        if feed_in_forecast:
            # Project the feed-in series onto the DP step windows using its own
            # start time. Resampling by interval alone assumed both series began
            # at the same instant, which silently shifted the feed-in prices by
            # up to one grid-price period whenever the two sensors publish at
            # different resolutions (e.g. hourly feed-in with 15-min prices).
            resampled_feed_in = resample_to_steps(
                feed_in_forecast,
                feed_in_start,
                feed_in_interval,
                step_starts,
                step_durations_hours,
            )
            if not resampled_feed_in:
                # A feed-in series that does not reach the first step window
                # yields an empty list. An empty feed-in forecast must
                # never reach the optimizer: each step would fall back to the
                # grid buy price and the terminal value of stored energy would
                # become 0, making PV arbitrage look unprofitable. Fall back to
                # the fixed feed-in price (same as an unavailable sensor).
                resampled_feed_in = [self._fixed_feed_in_price()] * len(
                    resampled_prices
                )

        # Project PV / consumption onto the DP step windows. The forecast
        # pipeline anchors its series to the current quarter hour, the DP steps
        # to price-period boundaries; with hourly prices those differ by up to
        # 45 minutes, so resampling by interval length alone shifted the whole
        # series (see resample_to_steps).
        fc_interval = int(forecast_data.get("forecast_interval_minutes", 60))
        fc_start = forecast_data.get("forecast_start_utc") or now_utc
        pv_forecast = resample_to_steps(
            forecast_data.get("pv_forecast_kw", []),
            fc_start,
            fc_interval,
            step_starts,
            step_durations_hours,
        )
        consumption_forecast = resample_to_steps(
            forecast_data.get("consumption_forecast_kw", []),
            fc_start,
            fc_interval,
            step_starts,
            step_durations_hours,
        )

        # Horizon = length of price forecast (the binding constraint)
        n_steps = len(resampled_prices)

        # Get DC-coupled PV forecast if available
        pv_dc_forecast = None
        if forecast_data.get("pv_dc_coupled"):
            raw_dc = forecast_data.get("pv_dc_forecast_kw", [])
            if raw_dc and any(v > 0 for v in raw_dc):
                pv_dc_forecast = resample_to_steps(
                    raw_dc, fc_start, fc_interval, step_starts, step_durations_hours
                )

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
            if feed_in_is_dynamic and self._feed_in_price_model.has_data():
                # Own feed-in model has historical data — use it directly.
                resampled_feed_in.extend(
                    self._model_extension(
                        self._feed_in_price_model,
                        len(resampled_feed_in),
                        steps_needed,
                        price_interval,
                    )
                )
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
            feed_in_price_forecast_model = self._model_reference_series(
                self._feed_in_price_model, len(resampled_prices), price_interval
            )

        # Get current battery state
        battery_state = self.get_current_battery_state()

        # Cumulative throughput counters, read once: they close the previous
        # planned step and open the new one at the same instant.
        energy_totals_now = self._read_energy_totals()

        # Charge efficiency calibration: compare the previous plan against what
        # the battery actually moved, to detect systematic over-estimation of
        # charge efficiency (e.g. CV-phase).
        self._update_charge_eff_calibration(battery_state, energy_totals_now)
        # Discharge efficiency calibration: same principle for discharging steps.
        self._update_discharge_eff_calibration(battery_state, energy_totals_now)

        battery_config = self.battery_config

        # Apply charge efficiency correction only to the charge-side SoC
        # transition: when the battery charges slower than modelled, the DP
        # should plan less charge within the step. Economic costs still use the
        # nominal curve so a charging-speed problem is not double-counted as
        # extra energy cost or degradation.
        charge_eff_curve_override: EfficiencyCurve | None = None
        if self._charge_eff_correction < 0.995:
            charge_eff_curve_override = [
                (p, min(1.0, eff * self._charge_eff_correction))
                for p, eff in battery_config.charge_efficiency_curve_parsed
            ]
            _LOGGER.debug(
                "Charge efficiency correction %.3f applied to curve",
                self._charge_eff_correction,
            )

        # Apply discharge efficiency correction only to the discharge-side SoC
        # transition: when the battery discharges slower than modelled, the DP
        # should plan less discharge within the step. Economic costs still use
        # the nominal curve.
        #
        # The SoC transition is: soc -= power * hours / discharge_eff
        # To reduce planned SoC drop by factor `correction`, we need a LARGER
        # discharge_eff (dividing by a larger value gives a smaller drop).
        # Hence we divide each curve point by the correction, not multiply.
        # Curve points may exceed 1.0 here; that is intentional — the override
        # only affects SoC state transitions, not the economic cost.
        discharge_eff_curve_override: EfficiencyCurve | None = None
        if self._discharge_eff_correction < 0.995:
            discharge_eff_curve_override = [
                (p, max(1e-6, eff / self._discharge_eff_correction))
                for p, eff in battery_config.discharge_efficiency_curve_parsed
            ]
            _LOGGER.debug(
                "Discharge efficiency correction %.3f applied to curve",
                self._discharge_eff_correction,
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
            charge_eff_curve_override,
            discharge_eff_curve_override,
        )

        self._last_result = result
        # Snapshot the counters alongside the plan they belong to, so the next
        # run can measure the throughput of exactly this step.
        self._energy_counter_snapshot = energy_totals_now

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

        effective_mode, effective_power = self._resolve_effective_mode(
            result, current_grid, resampled_feed_in, hybrid_deadband_w
        )
        (
            effective_mode,
            effective_power,
            commitment_locked,
            commitment_reason,
        ) = self._apply_commitment_filter(
            effective_mode,
            effective_power,
            battery_state=battery_state,
            battery_config=battery_config,
            current_price=resampled_prices[0] if resampled_prices else 0.0,
            current_step_start=current_step_start,
            degradation_cost_per_kwh=degradation_cost_per_kwh,
            min_spread=min_spread,
        )

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
                # The mode the real-time controller actually ran in. This can
                # differ from effective_mode: idle is upgraded to zero_grid on
                # PV surplus (see _resolve_controller_mode). In that case
                # effective_power_kw stays 0 by design while the controller
                # publishes a real setpoint, so consumers must not read the
                # difference as the battery having hit a limit.
                "controller_mode": controller_mode,
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
            combined_setpoint_kw, self._control_mode
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
            "current_feed_in_price": self._current_feed_in_price(resampled_feed_in),
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

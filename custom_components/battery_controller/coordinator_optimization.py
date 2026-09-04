"""Optimization coordinator for the Battery Controller integration."""

from __future__ import annotations

import asyncio
import logging
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

from .battery_dispatch import BatteryDispatcher
from .battery_model import BatteryConfig, BatteryState, aggregate_battery_configs
from .efficiency_calibration import (
    CALIBRATION_APPLY_THRESHOLD,
    CALIBRATION_DC_COUPLED,
    CALIBRATION_DELTA_TOO_SMALL,
    CALIBRATION_DERATING,
    CALIBRATION_NO_PLAN,
    CALIBRATION_NO_RESULT,
    CALIBRATION_NO_SOC_SOURCE,
    CALIBRATION_NOT_DISPATCHED,
    CALIBRATION_PLAN_NOT_EXECUTED,
    CALIBRATION_STEP_INCOMPLETE,
    CHARGE_CALIBRATION,
    DISCHARGE_CALIBRATION,
    BatteryCalibration,
    CalibrationSpec,
    DirectionCalibration,
    aggregate_correction,
    aggregate_last_result,
    counter_delta_kwh,
    curve_override,
    min_planned_delta_kwh,
)
from .efficiency_curve import interpolate_efficiency
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
    CONF_NAME,
    CONF_TIME_STEP_MINUTES,
    DEFAULT_TIME_STEP_MINUTES,
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
    GRID_READING_ALIVE_W,
    STALE_SENSOR_MIN_LIMIT_S,
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
    resample_to_steps,
    state_has_value,
    usable_state,
)
from .optimizer import optimize_battery_schedule, OptimizationResult
from .zero_grid_controller import create_zero_grid_controller

_LOGGER = logging.getLogger(__name__)

# Hybrid mode: when the DP plans charging while the grid is exporting, switch
# to zero_grid (surplus-following) only if the export surplus covers at least
# this fraction of the planned charge power. Below it, the DP evidently wants
# grid charging beyond the surplus and the schedule is followed instead.
_SURPLUS_COVERS_PLAN_FRACTION = 0.8


def _mode_from_power_kw(power_kw: float) -> str:
    """Classify a measured battery power as charging, discharging or idle."""
    power_w = power_kw * 1000
    if power_w > BATTERY_MODE_THRESHOLD_W:
        return ACTION_CHARGING
    if power_w < -BATTERY_MODE_THRESHOLD_W:
        return ACTION_DISCHARGING
    return ACTION_IDLE


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

        # Build per-battery configs and aggregate for optimizer. The dispatcher
        # owns the per-battery configs and states; the coordinator reaches them
        # through the properties below.
        self._dispatcher = BatteryDispatcher(
            [
                (sid, BatteryConfig.from_subentry(d))
                for sid, d in self._battery_subentries
            ]
        )
        self.battery_config = aggregate_battery_configs(
            [cfg for _, cfg in self._individual_battery_configs]
        )
        self._apply_entry_level_config()

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

        # Hysteresis state for the grid-charge economics test: tracks whether
        # buying the planned charge from the grid was last found worthwhile
        # ("charging") or not ("zero_grid"), so a shadow price hovering near the
        # buy price doesn't flip the mode every tick.
        self._last_grid_charge_decision: str = ACTION_CHARGING

        # Previous tick's summed grid reading, for the liveness check in
        # _handle_realtime_update. None until the first reading.
        self._last_grid_reading_w: float | None = None

        # Whether the current stale-sensor episode has already spent its one
        # allowed correction away from zero. Reset as soon as a sensor reports
        # again. See the stale-sensor handling in _handle_realtime_update.
        self._stale_escape_used: bool = False

        # Whether the hybrid charge arbitration refused grid import for the
        # planned charge (the DP planned a charge whose price the shadow price
        # does not justify — a planned PV charge the sun did not deliver). Set
        # both when the charge is dropped altogether and when it is only capped
        # at the available surplus. Lets the commitment filter release an active
        # charge instead of suppressing the veto until the end of the price
        # period, and keeps the raw plan as the setpoint the realtime loop
        # returns to once the surplus recovers.
        self._grid_charge_vetoed: bool = False

        # Charge setpoint the last run would apply when following the schedule,
        # in W. The realtime loop re-runs the charge arbitration on live sensor
        # values and needs the run's committed power to return to, not the raw
        # plan (which ignores the commitment filter).
        self._scheduled_charge_w: float = 0.0

        # Inputs of the last run's charge arbitration, so the realtime loop can
        # repeat it between runs without re-running the DP.
        self._last_feed_in_price: float = 0.0
        self._last_grid_price: float = 0.0
        self._last_degradation_cost_per_kwh: float = 0.0

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

        # Efficiency calibration, one per direction per battery: a rolling
        # window of (actual_delta / planned_delta) samples collected on steps
        # that planned that direction for that pack. The smoothed correction
        # scales the DP's SoC transition for that direction only — never the
        # economic cost model — so a charging-speed problem is not
        # double-counted as extra energy cost. Only non-DC-coupled systems are
        # sampled, to avoid confounding with passive PV. The DP plans one fleet
        # SoC, so it is handed the capacity-weighted aggregate of these.
        # See efficiency_calibration.py.
        self._battery_calibrations: dict[str, BatteryCalibration] = {
            sid: self._build_battery_calibration(sid, data)
            for sid, data in self._battery_subentries
        }

        # Cumulative throughput counters per battery as they stood when
        # _last_result was planned, and the SoC each pack held at that moment.
        # The difference against the next read is the energy that battery
        # actually moved over the step — a far finer measurement than the SoC
        # delta, which on a whole-percent sensor quantises at capacity/100
        # (0.1 kWh on a 10 kWh pack).
        self._energy_counter_snapshot: dict[str, dict[str, float | None]] = {}
        self._soc_snapshot_kwh: dict[str, float] = {}
        # The setpoint each battery was given for the step being scored. The
        # dispatcher concentrates on one pack at a time, so this is what says
        # whose plan the measured throughput belongs to.
        self._last_battery_setpoints: dict[str, float] = {}

    def _build_battery_calibration(
        self, subentry_id: str, data: dict[str, Any]
    ) -> BatteryCalibration:
        """Create one battery's two calibrations, with their own storage keys.

        ``legacy_store`` points at the fleet-wide key these used to share, so a
        system that has already learned something keeps it as a starting point
        instead of restarting from nominal. See DirectionCalibration.async_load.
        """
        entry_id = self.config.get("entry_id", "unknown")
        label = str(data.get(CONF_NAME) or subentry_id)

        def _for(spec: CalibrationSpec) -> DirectionCalibration:
            return DirectionCalibration(
                spec=spec,
                store=storage.Store(
                    self.hass,
                    1,
                    f"battery_controller_{entry_id}_{subentry_id}_{spec.name}_eff",
                ),
                label=label,
                legacy_store=storage.Store(
                    self.hass, 1, f"battery_controller_{entry_id}_{spec.name}_eff"
                ),
            )

        return BatteryCalibration(
            subentry_id=subentry_id,
            charge=_for(CHARGE_CALIBRATION),
            discharge=_for(DISCHARGE_CALIBRATION),
        )

    @property
    def battery_calibrations(self) -> dict[str, BatteryCalibration]:
        """Per-battery efficiency calibration, keyed by subentry id."""
        return self._battery_calibrations

    def battery_calibration_state(
        self, subentry_id: str, action: str
    ) -> tuple[float, int, bool, str]:
        """One battery's (correction, samples, applied, last_result).

        The entity-facing view. ``applied`` is per battery here — whether this
        pack's own measurement is far enough from nominal to matter — while the
        fleet sensors report whether the aggregate changes the DP's plan.
        """
        calibration = self._battery_calibrations.get(subentry_id)
        if calibration is None:
            return (1.0, 0, False, CALIBRATION_NO_RESULT)
        direction = calibration.for_action(action)
        return (
            direction.correction,
            direction.sample_count,
            direction.applied,
            direction.last_result,
        )

    def battery_dispatch_fidelity(
        self, subentry_id: str, action: str
    ) -> tuple[float | None, int]:
        """One battery's (measured/commanded throughput, samples) for a direction.

        None until the energy counters have measured a dispatched step. Reported
        rather than applied: it says whether the device followed its setpoint,
        which the correction cannot — see DirectionCalibration.record_dispatch.
        """
        calibration = self._battery_calibrations.get(subentry_id)
        if calibration is None:
            return (None, 0)
        direction = calibration.for_action(action)
        return (direction.dispatch_fidelity, direction.dispatch_sample_count)

    def dispatch_fidelity(self, action: str) -> tuple[float | None, int]:
        """The fleet's dispatch fidelity for one direction.

        A plain mean over the batteries that have measured something: unlike the
        correction this never reaches the DP, so there is no single figure it has
        to be faithful to — it only has to make a device that stopped following
        its setpoint visible.
        """
        measured = [
            (cal.dispatch_fidelity, cal.dispatch_sample_count)
            for cal in self._direction_calibrations(action)
            if cal.dispatch_fidelity is not None
        ]
        if not measured:
            return (None, 0)
        total = sum(value for value, _n in measured if value is not None)
        return (total / len(measured), sum(n for _v, n in measured))

    def battery_calibration_report(self) -> dict[str, dict[str, Any]]:
        """Every battery's calibration, for diagnostics."""
        report: dict[str, dict[str, Any]] = {}
        for sid, data in self._battery_subentries:
            calibration = self._battery_calibrations.get(sid)
            if calibration is None:
                continue
            entry: dict[str, Any] = {"name": data.get(CONF_NAME) or sid}
            for direction in calibration.both():
                fidelity = direction.dispatch_fidelity
                entry[direction.spec.name] = {
                    "correction": round(direction.correction, 4),
                    "samples": direction.sample_count,
                    "applied": direction.applied,
                    "last_result": direction.last_result,
                    "dispatch_fidelity": (
                        round(fidelity, 4) if fidelity is not None else None
                    ),
                    "dispatch_samples": direction.dispatch_sample_count,
                }
            report[sid] = entry
        return report

    def _direction_calibrations(self, action: str) -> list[DirectionCalibration]:
        """One direction's calibration for every battery, in configured order."""
        return [
            self._battery_calibrations[sid].for_action(action)
            for sid, _ in self._battery_subentries
            if sid in self._battery_calibrations
        ]

    def _aggregate_correction(self, action: str) -> float:
        """The fleet correction the DP plans with, for one direction."""
        capacity = {
            sid: cfg.capacity_kwh for sid, cfg in self._individual_battery_configs
        }
        weighted: list[tuple[float, float]] = []
        for sid, _ in self._battery_subentries:
            calibration = self._battery_calibrations.get(sid)
            if calibration is None:
                continue
            direction = calibration.for_action(action)
            # A pack that has never sampled carries no evidence, so it gets no
            # weight rather than a nominal 1.0 that would dilute its sibling.
            weight = capacity.get(sid, 0.0) if direction.sample_count else 0.0
            weighted.append((direction.correction, weight))
        return aggregate_correction(weighted)

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

        The fleet figure the DP plans with: the capacity-weighted aggregate of
        the per-battery corrections. Already published in ``data`` and in
        diagnostics; exposed here so the current value can be read without a
        completed run, the same way control_mode and optimization_enabled are.
        """
        return self._aggregate_correction(ACTION_CHARGING)

    @property
    def charge_eff_sample_count(self) -> int:
        """Observations behind charge_eff_correction, across all batteries."""
        return sum(
            cal.sample_count for cal in self._direction_calibrations(ACTION_CHARGING)
        )

    @property
    def discharge_eff_correction(self) -> float:
        """Learned multiplier on the discharge-side SoC transition."""
        return self._aggregate_correction(ACTION_DISCHARGING)

    @property
    def discharge_eff_sample_count(self) -> int:
        """Observations behind discharge_eff_correction, across all batteries."""
        return sum(
            cal.sample_count for cal in self._direction_calibrations(ACTION_DISCHARGING)
        )

    @property
    def charge_eff_applied(self) -> bool:
        """Whether the charge correction currently changes the DP's plan.

        The aggregate is only handed to the optimizer below 0.995: a value at or
        above that is within measurement noise of nominal and is stored but
        never applied. Same gate as curve_override(). Judged on the
        aggregate, because that is what the DP is given — a single derated pack
        can be applied on its own sensor and still leave the fleet at nominal.
        """
        return self.charge_eff_correction < CALIBRATION_APPLY_THRESHOLD

    @property
    def discharge_eff_applied(self) -> bool:
        """Whether the discharge correction currently changes the DP's plan."""
        return self.discharge_eff_correction < CALIBRATION_APPLY_THRESHOLD

    @property
    def charge_eff_last_result(self) -> str:
        """What the last charge-calibration attempt did, and why."""
        return aggregate_last_result(
            cal.last_result for cal in self._direction_calibrations(ACTION_CHARGING)
        )

    @property
    def discharge_eff_last_result(self) -> str:
        """What the last discharge-calibration attempt did, and why."""
        return aggregate_last_result(
            cal.last_result for cal in self._direction_calibrations(ACTION_DISCHARGING)
        )

    @property
    def _individual_battery_configs(self) -> list[tuple[str, BatteryConfig]]:
        """Per-battery configs, owned by the dispatcher."""
        return self._dispatcher.configs

    @_individual_battery_configs.setter
    def _individual_battery_configs(
        self, value: list[tuple[str, BatteryConfig]]
    ) -> None:
        self._dispatcher.configs = value

    @property
    def _per_battery_states(self) -> dict[str, BatteryState]:
        """Per-battery state cache, owned by the dispatcher."""
        return self._dispatcher.states

    @_per_battery_states.setter
    def _per_battery_states(self, value: dict[str, BatteryState]) -> None:
        self._dispatcher.states = value

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
        stale_limit_s = max(
            STALE_SENSOR_MULTIPLIER * self.zero_grid_controller.config.response_time_s,
            STALE_SENSOR_MIN_LIMIT_S,
        )
        grid_sensor_stale = self._find_stale_power_sensor(stale_limit_s) is not None
        # A sensor that has not reported is only untrustworthy if the reading it
        # feeds has stopped moving too. The common false positive is a sensor
        # that is legitimately constant: an import meter reads exactly 0 W for
        # as long as the house exports, so an on-change source stops publishing
        # it and the whole set is flagged — during precisely the export the
        # loop is supposed to be correcting (issue #174). A summed reading that
        # keeps changing is proof the set is alive, and a frozen sum is the
        # runaway condition the guard actually cares about, so it is the better
        # signal on both counts.
        if (
            grid_sensor_stale
            and self._last_grid_reading_w is not None
            and abs(current_grid_w - self._last_grid_reading_w) >= GRID_READING_ALIVE_W
        ):
            grid_sensor_stale = False
        self._last_grid_reading_w = current_grid_w

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
            if (
                self._control_mode in (MODE_HYBRID, MODE_HYBRID_PLUS)
                and self._last_result.optimal_mode == ACTION_CHARGING
            ):
                # A planned charge is the one decision that cannot wait for the
                # next run: it is taken on forecast PV, and when that PV does
                # not arrive the schedule imports from the grid until the price
                # period ends (issue #174). The arbitration itself is a handful
                # of comparisons on the cached plan — no DP — so it is repeated
                # here on live sensor values. Everything else stays cached.
                rt_effective_mode, rt_power_kw = self._resolve_charge_mode(
                    self._last_result,
                    self._net_surplus_w(current_grid_w, battery_state.power_kw * 1000),
                    self._last_feed_in_price,
                    self._last_grid_price,
                    self._last_degradation_cost_per_kwh,
                    self._hybrid_deadband_w(),
                )
                controller_schedule_w = (
                    # Return to the run's committed power, not the raw plan:
                    # the commitment filter may have capped it. The arbitration
                    # can cap it further (a charge trimmed to the surplus at a
                    # negative feed-in price), so take whichever is lower.
                    min(self._scheduled_charge_w, rt_power_kw * 1000)
                    if rt_effective_mode == ACTION_CHARGING
                    else rt_power_kw * 1000
                )

        controller_mode = self._resolve_controller_mode(
            rt_effective_mode, current_grid_w, battery_state.power_kw * 1000
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
        # A genuinely dead sensor stays frozen, though, so a move AWAY from zero —
        # a larger charge/discharge, or a reversal — is rationed: one per stale
        # episode, then the setpoint holds until a sensor reports again.
        #
        # Refusing those moves outright deadlocks (issue #174). The grid can sit
        # far from zero while the setpoint sits near it — a correction the
        # downstream inverter or automation ignored, say — and then every input
        # is frozen: the battery does not move, so the grid does not move, so an
        # on-change sensor never reports, so the reading stays "stale" and the
        # correction that would fix it is exactly the one being refused. One
        # bounded step breaks that, because acting moves the grid, which is what
        # wakes the sensor. If it does not wake, nothing further happens.
        if grid_sensor_stale:
            prev_w = (
                self.data.get("control_action", {}).get("target_power_w", 0.0)
                if self.data
                else 0.0
            )
            new_w = control_action["target_power_w"]
            moves_away_from_zero = abs(new_w) > abs(prev_w) or (new_w * prev_w < 0)
            if moves_away_from_zero and self._stale_escape_used:
                # This episode already spent its step — restore and hold.
                self.zero_grid_controller.reset_setpoint(saved_last_target_w)
                _LOGGER.debug(
                    "Grid power sensor stale; holding zero_grid setpoint "
                    "(rejected %.0f W -> %.0f W, away from zero, escape spent)",
                    prev_w,
                    new_w,
                )
                return
            if moves_away_from_zero:
                self._stale_escape_used = True
                _LOGGER.debug(
                    "Grid power sensor stale; allowing one correction away from "
                    "zero (%.0f W -> %.0f W) to break a possible deadlock",
                    prev_w,
                    new_w,
                )
        else:
            # A sensor reported again: the next episode starts with its step.
            self._stale_escape_used = False

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
                        "updated_at": dt_util.now().isoformat(),
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
                # When this snapshot was taken. `timestamp` marks the last full
                # optimizer run; this marks the last publish of any kind, so a
                # consumer can tell a live reading from one the loop stopped
                # refreshing (every early return below leaves the previous
                # values in place, including battery_state).
                "updated_at": dt_util.now().isoformat(),
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

    @staticmethod
    def _net_surplus_w(current_grid_w: float, battery_power_w: float) -> float:
        """PV surplus of the house in W, independent of what the battery does.

        The grid meter reads ``load + battery - pv``, so the surplus the house
        actually has is ``pv - load = battery - grid``. Measuring it that way
        matters for every hybrid decision: the raw meter reading includes the
        battery's own action, so a charge started from the DP schedule drives
        the meter positive and makes the surplus look gone — the arbitration
        that should downgrade that charge to zero_grid then never runs again
        (it latches), and a zero_grid capture holding the meter at ~0 W keeps
        reading as "surplus" long after the sun stopped delivering one.

        Positive = PV exceeds consumption (exportable), negative = the house is
        short and needs grid or battery power.

        Args:
            current_grid_w: Grid power in W (positive = import).
            battery_power_w: Battery power in W (positive = charge).
        """
        return battery_power_w - current_grid_w

    def _should_charge_from_grid(
        self,
        shadow_price_eur_kwh: float,
        current_price: float,
        charge_kw: float,
        degradation_cost_per_kwh: float,
    ) -> bool:
        """Return whether buying the planned charge from the grid is worth it.

        The mirror image of _should_capture_surplus, against the buy price
        instead of the feed-in price. Importing 1 kWh of AC stores
        ``charge_eff`` kWh, so a stored kWh costs ``price / charge_eff`` plus
        the degradation of putting it through the battery. It is worth buying
        while the DP shadow price λ — the marginal value of a stored kWh given
        the whole price and PV forecast — covers that.

        This is what separates the two reasons the DP plans a charge. Planned
        grid arbitrage (a cheap hour) keeps λ above the cost of buying, so the
        schedule is followed and the import is deliberate. A charge planned
        against forecast PV that failed to show up does not: λ then reflects
        that the battery can be filled from the sun later, well below what the
        grid charges now, and importing to keep the plan on paper is a real
        loss (issue #174).

        Applies the same ±5% hysteresis band as the other hybrid thresholds so
        a shadow price hovering near the buy price doesn't flip the decision.

        Args:
            shadow_price_eur_kwh: DP shadow price λ (value per stored kWh).
            current_price: Grid buy price for the step being executed.
            charge_kw: Planned charge power, used to price the charge at its
                curve efficiency instead of a nominal scalar.
            degradation_cost_per_kwh: Battery wear per kWh stored.
        """
        charge_eff = self._charge_efficiency_at(charge_kw)
        cost_per_stored_kwh = (
            current_price / charge_eff if charge_eff > 0 else current_price
        ) + degradation_cost_per_kwh
        if cost_per_stored_kwh <= 0:
            # Free or paid-for import (negative prices): always worth it, and
            # the ±5% band is meaningless on a negative threshold.
            self._last_grid_charge_decision = ACTION_CHARGING
            return True
        if self._last_grid_charge_decision == ACTION_CHARGING:
            worthwhile = bool(shadow_price_eur_kwh >= cost_per_stored_kwh * 0.95)
        else:
            worthwhile = bool(shadow_price_eur_kwh >= cost_per_stored_kwh * 1.05)
        self._last_grid_charge_decision = ACTION_CHARGING if worthwhile else "zero_grid"
        return worthwhile

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
        self,
        effective_mode: str,
        current_grid_w: float,
        battery_power_w: float = 0.0,
    ) -> str:
        """Map effective mode to zero_grid_controller mode.

        For idle mode with PV surplus, upgrades to zero_grid when real-time
        power sensors are available. The surplus is measured as
        ``battery - grid`` (see _net_surplus_w), so it stays valid once the
        upgrade takes effect: a zero_grid capture holds the meter near 0 W,
        which on the raw reading is indistinguishable from "no surplus left".
        The decision is damped with a band of ±zero_grid_deadband_w (the
        user-tunable number entity, default 50 W) around the 0 W crossing,
        remembered across calls: enter zero_grid only once the surplus exceeds
        the band, then stay there until it goes solidly negative.

        Without that band sensor noise around the crossing would flip the mode
        at tick rate; the setpoint deadband does not damp that, because the
        swing between the zero_grid setpoint and idle's 0 W is far larger than
        the deadband.

        The upgrade is suppressed while the last optimizer run found surplus
        capture uneconomical (exporting is worth more than storing per the
        shadow price), so idle genuinely means "export the surplus" — otherwise
        the realtime loop would re-capture the surplus the run just refused.

        Args:
            effective_mode: The resolved mode from optimization logic.
            current_grid_w: Current grid power in W (positive = import).
            battery_power_w: Current battery power in W (positive = charge).

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
            surplus_w = self._net_surplus_w(current_grid_w, battery_power_w)
            if self._last_idle_upgrade_decision == "zero_grid":
                # Was capturing surplus; keep doing so until it has genuinely
                # disappeared (the house draws more than the PV delivers).
                has_pv_surplus = surplus_w > -band
            else:
                # Was idle; only start capturing once the surplus is solid, so
                # near-zero sensor noise does not trigger it.
                has_pv_surplus = surplus_w > band
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
        """Split the combined setpoint across batteries (see BatteryDispatcher)."""
        return self._dispatcher.split_setpoint(total_kw, mode)

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

    async def _async_load_charge_eff_calibration(self) -> None:
        """Load every battery's persisted charge calibration from storage."""
        await asyncio.gather(
            *(cal.async_load() for cal in self._direction_calibrations(ACTION_CHARGING))
        )

    async def _async_save_charge_eff_calibration(self) -> None:
        """Persist every battery's charge calibration to storage."""
        await asyncio.gather(
            *(cal.async_save() for cal in self._direction_calibrations(ACTION_CHARGING))
        )

    async def async_reset_charge_eff_calibration(self) -> None:
        """Reset every battery's charge calibration to the nominal state."""
        await asyncio.gather(
            *(
                cal.async_reset()
                for cal in self._direction_calibrations(ACTION_CHARGING)
            )
        )

    async def _async_load_discharge_eff_calibration(self) -> None:
        """Load every battery's persisted discharge calibration from storage."""
        await asyncio.gather(
            *(
                cal.async_load()
                for cal in self._direction_calibrations(ACTION_DISCHARGING)
            )
        )

    async def _async_save_discharge_eff_calibration(self) -> None:
        """Persist every battery's discharge calibration to storage."""
        await asyncio.gather(
            *(
                cal.async_save()
                for cal in self._direction_calibrations(ACTION_DISCHARGING)
            )
        )

    async def async_reset_discharge_eff_calibration(self) -> None:
        """Reset every battery's discharge calibration to the nominal state."""
        await asyncio.gather(
            *(
                cal.async_reset()
                for cal in self._direction_calibrations(ACTION_DISCHARGING)
            )
        )

    def _read_counter_kwh(self, sensor_id: str) -> float | None:
        """Read one cumulative energy counter, in kWh."""
        state = usable_state(self.hass, sensor_id)
        if state is None:
            return None
        try:
            value = float(state.state)
        except (ValueError, TypeError):
            return None
        unit = str(state.attributes.get("unit_of_measurement") or "kWh")
        if unit == "Wh":
            return value / 1000.0
        if unit == "MWh":
            return value * 1000.0
        if unit != "kWh":
            self._warn_unit_once(sensor_id, unit, "treating value as kWh")
        return value

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
            value = self._read_counter_kwh(sensor_id)
            if value is None:
                return None
            total += value
        return total

    def _read_energy_totals(self) -> dict[str, dict[str, float | None]]:
        """Read every battery's throughput counters at one instant.

        Keyed by subentry, because a fleet sum cannot say which pack moved the
        energy — and with the dispatcher concentrating on one battery at a time,
        that is exactly the question the calibration asks.
        """
        return {
            sid: {
                "charged": self._read_counter_kwh(
                    data.get(CONF_BATTERY_ENERGY_CHARGED_SENSOR) or ""
                )
                if data.get(CONF_BATTERY_ENERGY_CHARGED_SENSOR)
                else None,
                "discharged": self._read_counter_kwh(
                    data.get(CONF_BATTERY_ENERGY_DISCHARGED_SENSOR) or ""
                )
                if data.get(CONF_BATTERY_ENERGY_DISCHARGED_SENSOR)
                else None,
            }
            for sid, data in self._battery_subentries
        }

    def _battery_soc_quantum_kwh(
        self, data: dict[str, Any], cfg: BatteryConfig
    ) -> float:
        """Resolution of one battery's SoC measurement, in kWh.

        Derived from the decimals the SoC sensor actually reports: a state of
        "47" is whole-percent, "47.3" is ten times finer. Returns 0.0 when
        nothing can be determined, which leaves the caller on its fixed floor.
        """
        sensor_id = data.get(CONF_BATTERY_SOC_SENSOR)
        if not sensor_id:
            return 0.0
        state = usable_state(self.hass, sensor_id)
        if state is None:
            return 0.0
        raw = state.state.strip()
        decimals = len(raw.split(".", 1)[1]) if "." in raw else 0
        step = 10.0**-decimals
        unit = str(state.attributes.get("unit_of_measurement") or "%")
        if unit == "kWh":
            return step
        if unit == "Wh":
            return step / 1000.0
        # percent (or unitless, which is treated as percent elsewhere)
        return step / 100.0 * cfg.capacity_kwh

    def _soc_quantum_kwh(self) -> float:
        """Estimate the resolution of the aggregated SoC measurement, in kWh.

        Summed across packs because the aggregate SoC is a sum and the
        individual quantisation errors can align.
        """
        return sum(
            self._battery_soc_quantum_kwh(data, cfg)
            for (_sid, cfg), (_sid2, data) in zip(
                self._individual_battery_configs, self._battery_subentries
            )
        )

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

    def _fleet_calibration_block(self, action: str) -> str | None:
        """Why no battery can be sampled for ``action`` this run, if any.

        These conditions are properties of the plan and of the system as a
        whole, so they are answered once rather than per battery.
        """
        result = self._last_result
        if result is None:
            return CALIBRATION_NO_RESULT
        if len(result.mode_schedule) < 1 or len(result.soc_schedule_kwh) < 2:
            return CALIBRATION_NO_RESULT
        if result.mode_schedule[0] != action:
            return CALIBRATION_NO_PLAN
        # Only sample when the plan was actually commanded; mode resolution
        # (hybrid → zero_grid, commitment filter, zero_grid/manual control)
        # would otherwise drag the correction towards the 0.5 clip floor.
        if not self._planned_first_step_was_executed(action):
            return CALIBRATION_PLAN_NOT_EXECUTED
        if not self._previous_step_complete():
            return CALIBRATION_STEP_INCOMPLETE
        if self.config.get(CONF_PV_DC_COUPLED, False):
            return CALIBRATION_DC_COUPLED
        return None

    def _previous_step_hours(self) -> float:
        """Length of the step being scored, in hours.

        Falls back to the coordinator's own interval when the plan carried no
        timing metadata — the same fallback _previous_step_complete makes.
        """
        if isinstance(self.data, dict):
            durations = self.data.get("step_durations_hours")
            if durations:
                try:
                    return float(durations[0])
                except (TypeError, ValueError, IndexError):
                    pass
        return (
            float(self.config.get(CONF_TIME_STEP_MINUTES, DEFAULT_TIME_STEP_MINUTES))
            / 60.0
        )

    def _planned_battery_delta_kwh(
        self, spec: CalibrationSpec, setpoint_kw: float, cfg: BatteryConfig
    ) -> float:
        """SoC change this battery was expected to make over the step, in kWh.

        Derived from the setpoint the dispatcher actually gave this pack and
        this pack's own efficiency curve at that power — not from the fleet SoC
        schedule. The dispatcher concentrates a setpoint on one battery at a
        time, so the fleet plan says nothing about what any single pack was
        asked to do, and the curves are power-dependent, so a battery running at
        1.2 kW is not modelled by the fleet's 2.4 kW point.
        """
        energy_kwh = abs(setpoint_kw) * self._previous_step_hours()
        curve = (
            cfg.charge_efficiency_curve_parsed
            if spec.action == ACTION_CHARGING
            else cfg.discharge_efficiency_curve_parsed
        )
        scalar = (
            cfg.charge_efficiency
            if spec.action == ACTION_CHARGING
            else cfg.discharge_efficiency
        )
        efficiency = (
            interpolate_efficiency(curve, abs(setpoint_kw)) if curve else float(scalar)
        )
        if efficiency <= 0:
            return 0.0
        # Charging puts less into the pack than it draws; discharging takes more
        # out of the pack than it delivers.
        return (
            energy_kwh * efficiency
            if spec.action == ACTION_CHARGING
            else energy_kwh / efficiency
        )

    def _update_eff_calibration(
        self,
        spec: CalibrationSpec,
        energy_totals_now: dict[str, dict[str, float | None]] | None,
    ) -> None:
        """Score the previous plan against reality, per battery, and fold it in.

        This method decides *eligibility* and records why on each battery's
        calibration; DirectionCalibration.record does the arithmetic and the
        persistence.

        Both directions ask the same question: did this battery move as much
        energy within the step as the plan assumed? A sample is only taken when:
        - The previous optimizer step planned this direction actively
        - The planned step was actually commanded unchanged (no hybrid/zero_grid
          override, no commitment-filter power lock)
        - The full planned step has elapsed
        - DC-coupled PV is not active: passive PV charging inflates the measured
          charge delta and offsets the measured discharge delta
        - The dispatcher gave *this* battery a setpoint in this direction. A
          pack that sat idle while its sibling did the work has nothing to say
          about its own efficiency, and attributing the sibling's throughput to
          it is what a single fleet-wide ratio used to do.
        - The planned delta is large enough to be measurable at the resolution
          of whatever measured it (energy counter or SoC sensor)
        - The step does not cross that battery's derating threshold. The plan
          holds full power for the whole step while the inverter throttles once
          the threshold is passed mid-step, so the ratio would reflect the
          derating rather than an efficiency loss.

        Each battery's correction is the mean of its last CALIBRATION_WINDOW
        ratios, clamped for use; the DP is handed their capacity-weighted
        aggregate. It scales the SoC transition for this direction only; the
        economic cost model is untouched.

        When no sample is taken, ``last_result`` names the reason. Several of
        those reasons are permanent for a given setup (a DC-coupled system never
        samples at all, and a control mode that does not execute the DP plan
        never will either), so the reason is published rather than leaving the
        user with a sample count stuck at zero.
        """
        blocked = self._fleet_calibration_block(spec.action)
        if blocked is not None:
            for calibration in self._direction_calibrations(spec.action):
                calibration.last_result = blocked
            return

        for (sid, cfg), (_sid, data) in zip(
            self._individual_battery_configs, self._battery_subentries
        ):
            battery = self._battery_calibrations.get(sid)
            if battery is None:
                continue
            self._update_battery_eff_calibration(
                battery.for_action(spec.action),
                sid,
                cfg,
                data,
                (energy_totals_now or {}).get(sid),
            )

    def _update_battery_eff_calibration(
        self,
        calibration: DirectionCalibration,
        subentry_id: str,
        cfg: BatteryConfig,
        data: dict[str, Any],
        counters_now: dict[str, float | None] | None,
    ) -> None:
        """Fold one battery's outcome for one direction into its correction."""
        spec = calibration.spec
        setpoint_kw = self._last_battery_setpoints.get(subentry_id, 0.0)
        # Setpoints are positive for charge, negative for discharge.
        dispatched = (
            setpoint_kw > 0 if spec.action == ACTION_CHARGING else setpoint_kw < 0
        )
        if not dispatched:
            calibration.last_result = CALIBRATION_NOT_DISPATCHED
            return

        planned_delta = self._planned_battery_delta_kwh(spec, setpoint_kw, cfg)
        prev_soc = self._soc_snapshot_kwh.get(subentry_id)
        planned_next_soc: float | None = None
        if prev_soc is not None:
            planned_next_soc = (
                prev_soc + planned_delta
                if spec.action == ACTION_CHARGING
                else prev_soc - planned_delta
            )

        # The energy counters answer a different question than the SoC sensor.
        # A counter sits on the same side of the inverter as the setpoint that
        # drove it, so measured-over-commanded is dispatch fidelity, not
        # efficiency; the SoC delta is the only measurement that spans the
        # conversion and can price it. The counter reading is therefore recorded
        # as a diagnostic and kept out of the correction entirely.
        snapshot = self._energy_counter_snapshot.get(subentry_id) or {}
        counter_delta = (
            counter_delta_kwh(
                spec.counter_key,
                snapshot.get(spec.counter_key),
                counters_now.get(spec.counter_key),
            )
            if counters_now is not None
            else None
        )
        if counter_delta is not None:
            calibration.record_dispatch(
                abs(setpoint_kw) * self._previous_step_hours(), counter_delta
            )

        if planned_delta < min_planned_delta_kwh(
            self._battery_soc_quantum_kwh(data, cfg)
        ):
            # Too small to measure reliably at this resolution; skip.
            calibration.last_result = CALIBRATION_DELTA_TOO_SMALL
            return

        if spec.derate_limit_kw(cfg) > 0 and planned_next_soc is not None:
            threshold_kwh = spec.derate_threshold_pct(cfg) / 100 * cfg.capacity_kwh
            if (
                min(prev_soc or 0.0, planned_next_soc)
                < threshold_kwh
                <= max(prev_soc or 0.0, planned_next_soc)
            ):
                _LOGGER.debug(
                    "%s efficiency calibration: skipping sample — step crosses "
                    "%s derating threshold (%.1f%% / %.2f kWh)",
                    calibration.who,
                    spec.derate_label,
                    spec.derate_threshold_pct(cfg),
                    threshold_kwh,
                )
                calibration.last_result = CALIBRATION_DERATING
                return

        if prev_soc is None:
            calibration.last_result = CALIBRATION_NO_RESULT
            return
        state = self._per_battery_states.get(subentry_id)
        if state is None:
            calibration.last_result = (
                CALIBRATION_NO_SOC_SOURCE
                if counter_delta is not None
                else CALIBRATION_NO_RESULT
            )
            return
        measured = (
            state.soc_kwh - prev_soc
            if spec.action == ACTION_CHARGING
            else prev_soc - state.soc_kwh
        )
        actual_delta = max(0.0, measured)

        if calibration.record(planned_delta, actual_delta, "SoC delta"):
            self.hass.async_create_task(calibration.async_save())

    def _update_charge_eff_calibration(
        self,
        energy_totals_now: dict[str, dict[str, float | None]] | None = None,
    ) -> None:
        """Fold the previous charging step's outcome into the charge corrections."""
        self._update_eff_calibration(CHARGE_CALIBRATION, energy_totals_now)

    def _update_discharge_eff_calibration(
        self,
        energy_totals_now: dict[str, dict[str, float | None]] | None = None,
    ) -> None:
        """Fold the previous discharging step's outcome into the discharge corrections."""
        self._update_eff_calibration(DISCHARGE_CALIBRATION, energy_totals_now)

    def _weather_hour_offset(self, weather: dict[str, Any], target: datetime) -> int:
        """Index into the weather series for the hour containing ``target``.

        The weather series is anchored to the hour of its own last fetch, which
        is up to one refresh interval behind the optimizer run. Deriving the
        offset from that anchor — rather than assuming element 0 is the current
        hour — keeps the model's GHI/wind features on the hours they describe.
        """
        start = weather.get("forecast_start_utc")
        if isinstance(start, str):
            start = dt_util.parse_datetime(start)
        if not isinstance(start, datetime):
            # No anchor published: fall back to the current hour, which is what
            # the weather coordinator writes when it does publish one.
            start = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
        offset = int(
            (dt_util.as_utc(target) - dt_util.as_utc(start)).total_seconds() // 3600
        )
        return max(0, offset)

    def _model_series(
        self,
        model: PriceForecastModel,
        step_starts: list[datetime],
        step_durations_hours: list[float],
    ) -> list[float]:
        """Project the historical model's hourly prediction onto step windows.

        The model is asked for the hours the steps actually occupy, its weather
        features are sliced to the same instant, and its hourly output is
        projected back onto the step windows. Anchoring on real timestamps is
        what keeps the prediction on the hours it describes: deriving the anchor
        from a step count instead placed the series up to a full hour late
        whenever the steps did not start on the hour — which is every run with a
        15-minute price sensor, and moved the next day's charge and discharge
        windows with it.
        """
        if not step_starts:
            return []
        weather = self.weather_coordinator.data or {}
        # The model buckets by local hour-of-day and weekday, so the anchor is
        # the start of the local hour containing the first step.
        model_start = dt_util.as_local(step_starts[0]).replace(
            minute=0, second=0, microsecond=0
        )
        last_duration = step_durations_hours[-1] if step_durations_hours else 1.0
        horizon_end = step_starts[-1] + timedelta(hours=last_duration)
        span_s = (horizon_end - model_start).total_seconds()
        total_hours = max(1, int(-(-span_s // 3600)))  # ceiling
        offset = self._weather_hour_offset(weather, model_start)
        ghi = weather.get("radiation_forecast", [])
        wind = weather.get("wind_speed_forecast", [])
        raw = model.forecast(
            hours=total_hours,
            start_time=model_start,
            ghi_forecast=ghi[offset:] if ghi else None,
            wind_forecast=wind[offset:] if wind else None,
        )
        series = resample_to_steps(
            raw, model_start, 60, step_starts, step_durations_hours
        )
        # A step past the end of the model output keeps the last known value:
        # both callers need one value per step, and the model can only run out
        # at the very end of the horizon.
        while len(series) < len(step_starts):
            series.append(series[-1] if series else 0.0)
        return series[: len(step_starts)]

    def _model_extension(
        self,
        model: PriceForecastModel,
        extension_start: datetime,
        steps_needed: int,
        interval_minutes: int,
    ) -> list[float]:
        """Extend a price series past the live forecast with a historical model.

        ``extension_start`` is the start of the first step the extension fills;
        the steps after it follow at one price period each.
        """
        step_starts = [
            extension_start + timedelta(minutes=i * interval_minutes)
            for i in range(steps_needed)
        ]
        return self._model_series(
            model, step_starts, [interval_minutes / 60.0] * steps_needed
        )

    def _resolve_effective_mode(
        self,
        result: OptimizationResult,
        current_grid: float,
        resampled_feed_in: list[float] | None,
        hybrid_deadband_w: float,
        battery_power_w: float = 0.0,
        current_price: float = 0.0,
        degradation_cost_per_kwh: float = 0.0,
    ) -> tuple[str, float]:
        """Turn the DP schedule into what the controller should actually do.

        follow_schedule executes the plan verbatim; zero_grid and manual ignore
        it; hybrid and hybrid+ arbitrate between the plan and surplus-following
        per step. Every branch is damped with hysteresis so a value hovering at
        a threshold does not flip the mode on every run.

        All hybrid branches judge the PV surplus as ``battery - grid``
        (_net_surplus_w) rather than as the raw meter reading, so the battery's
        own action does not mask the surplus it is running on.

        Returns:
            (effective_mode, effective_power_kw)
        """
        # Only the hybrid charge branch below can raise the veto; clearing it
        # here keeps it from outliving the run that decided it.
        self._grid_charge_vetoed = False
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
            # The surplus every branch below reasons about: what the house has
            # spare regardless of what the battery is doing with it.
            surplus_w = self._net_surplus_w(current_grid, battery_power_w)
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
                # zero-grid deadband) around the 0 W crossing so a surplus
                # hovering near zero (e.g. consumption ≈ PV production) doesn't
                # flip idle/zero_grid every realtime tick.
                if self._last_hybrid_idle_decision == "zero_grid":
                    # Was capturing surplus; keep doing so until it has
                    # genuinely disappeared.
                    has_pv_surplus = surplus_w > -hybrid_deadband_w
                else:
                    # Was idle; only start capturing once surplus is solid.
                    has_pv_surplus = surplus_w > hybrid_deadband_w
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
                        max(0.0, surplus_w) / 1000.0,
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
                        max(0.0, surplus_w) / 1000.0,
                    )
                    effective_mode = "zero_grid" if capture else ACTION_IDLE
                    effective_power = 0.0
                    self._last_hybrid_decision = effective_mode
                    self._surplus_capture_blocked = not capture
            elif result.optimal_mode == ACTION_CHARGING:
                effective_mode, effective_power = self._resolve_charge_mode(
                    result,
                    surplus_w,
                    self._current_feed_in_price(resampled_feed_in),
                    current_price,
                    degradation_cost_per_kwh,
                    hybrid_deadband_w,
                )
            else:
                effective_mode = result.optimal_mode
                effective_power = result.optimal_power_kw
        else:
            # follow_schedule: execute DP schedule exactly
            effective_mode = result.optimal_mode
            effective_power = result.optimal_power_kw

        return effective_mode, effective_power

    def _resolve_charge_mode(
        self,
        result: OptimizationResult,
        surplus_w: float,
        current_feed_in: float,
        current_price: float,
        degradation_cost_per_kwh: float,
        deadband_w: float,
    ) -> tuple[str, float]:
        """Arbitrate a planned charge between the DP schedule and the surplus.

        Runs whichever way the grid meter points. The earlier version only ran
        while the grid was exporting, which left the one case that needs it
        uncovered: a charge planned against forecast PV that failed to arrive
        imports from the grid, which makes the meter positive, which skipped
        this arbitration entirely and followed the plan to the end of the price
        period (issue #174).

        In order:
        1. Negative feed-in — exporting costs money, so keep a commanded charge
           (curtailing PV would otherwise deadlock zero_grid at ~0 W), capped at
           the surplus so it is still paid for by the sun and not by the grid.
        2. The surplus covers (most of) the plan — zero_grid tracks the real
           surplus instead of a fixed setpoint that imports whenever a cloud
           passes.
        3. Buying the rest is worth it per the shadow price — follow the
           schedule; this is the planned grid arbitrage the DP asked for.
        4. Otherwise the import is not justified: capture whatever surplus
           there is (zero_grid), or hold at idle when there is none.

        Both coverage and surplus tests carry the usual hysteresis so a value
        hovering at a threshold doesn't flip the mode every tick.

        Args:
            result: The optimization result being executed.
            surplus_w: House PV surplus in W (see _net_surplus_w).
            current_feed_in: Feed-in price for the step being executed.
            current_price: Grid buy price for the step being executed.
            degradation_cost_per_kwh: Battery wear per kWh stored.
            deadband_w: Hysteresis band in W (the zero-grid deadband).

        Returns:
            (effective_mode, effective_power_kw)
        """
        plan_w = result.optimal_power_kw * 1000
        if current_feed_in < 0 and surplus_w > 0:
            # Negative feed-in: exporting costs money. Keep a commanded charge
            # rather than zero_grid, so curtailing PV (grid → ~0) doesn't cause
            # a zero_grid deadlock that stops charging.
            #
            # The command is capped at the surplus the house actually has,
            # though. A negative feed-in price says exporting is worthless; it
            # says nothing about what importing costs, and the buy price is
            # usually still positive. Following the plan verbatim on a surplus
            # that is short of it therefore buys the difference — the case
            # reported on issue #174: a planned PV charge held at full power
            # through scattered cloud, importing at the buy price the whole
            # time. Rule 3 is what decides that buying is justified, so it is
            # consulted before the cap applies.
            if surplus_w >= plan_w or self._should_charge_from_grid(
                result.shadow_price_eur_kwh,
                current_price,
                result.optimal_power_kw,
                degradation_cost_per_kwh,
            ):
                self._last_hybrid_charge_decision = ACTION_CHARGING
                return ACTION_CHARGING, result.optimal_power_kw
            # Charging exactly the surplus is self-correcting: the grid settles
            # at ~0 W, and as PV recovers the surplus (battery - grid) grows
            # with it, so the setpoint climbs back to the plan on its own.
            self._grid_charge_vetoed = True
            _LOGGER.debug(
                "Hybrid charge capped to surplus at negative feed-in: "
                "plan %.0f W, surplus %.0f W, price %.3f, shadow price %.3f",
                plan_w,
                surplus_w,
                current_price,
                result.shadow_price_eur_kwh,
            )
            self._last_hybrid_charge_decision = ACTION_CHARGING
            return ACTION_CHARGING, surplus_w / 1000

        # Hysteresis: apply a ±5% band around the coverage threshold (same
        # pattern as the discharge decision) so PV surplus hovering near
        # _SURPLUS_COVERS_PLAN_FRACTION of the planned charge power doesn't
        # flip zero_grid/charging every tick.
        # • Was zero_grid → stay unless surplus drops below 95% of threshold
        # • Was charging → switch only once surplus reaches 105% of threshold
        if self._last_hybrid_charge_decision == "zero_grid":
            coverage_threshold = _SURPLUS_COVERS_PLAN_FRACTION * 0.95
        else:
            coverage_threshold = _SURPLUS_COVERS_PLAN_FRACTION * 1.05
        if surplus_w >= plan_w * coverage_threshold:
            # PV surplus covers (most of) the planned charge: use zero_grid to
            # dynamically match the actual surplus instead of fixed-rate
            # charging. Fixed charging may import from grid when clouds pass.
            self._last_hybrid_charge_decision = "zero_grid"
            return "zero_grid", 0.0

        if self._should_charge_from_grid(
            result.shadow_price_eur_kwh,
            current_price,
            result.optimal_power_kw,
            degradation_cost_per_kwh,
        ):
            # The DP planned substantially more charging than the current
            # surplus and the shadow price justifies buying the difference —
            # e.g. a cheap price hour that happens to coincide with a small PV
            # surplus. Zero_grid would forfeit that arbitrage, so follow the
            # schedule.
            self._last_hybrid_charge_decision = ACTION_CHARGING
            return ACTION_CHARGING, result.optimal_power_kw

        # The plan counted on PV that is not there, and grid power is worth
        # less stored than it costs to buy. Never import for it: charge on
        # whatever surplus exists, and hold when there is none. The DP charges
        # later, when the sun (or a cheaper hour) delivers.
        self._grid_charge_vetoed = True
        if self._last_hybrid_charge_decision == "zero_grid":
            surplus_present = surplus_w > -deadband_w
        else:
            surplus_present = surplus_w > deadband_w
        _LOGGER.debug(
            "Hybrid charge veto: plan %.0f W, surplus %.0f W, price %.3f, "
            "shadow price %.3f -> %s",
            plan_w,
            surplus_w,
            current_price,
            result.shadow_price_eur_kwh,
            "zero_grid" if surplus_present else ACTION_IDLE,
        )
        self._last_hybrid_charge_decision = (
            "zero_grid" if surplus_present else ACTION_IDLE
        )
        return ("zero_grid", 0.0) if surplus_present else (ACTION_IDLE, 0.0)

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
                # A vetoed grid charge is not the 15-min re-planning jitter the
                # commitment exists to damp — it is a measurement saying the
                # plan's PV never arrived. Holding it would import for the rest
                # of the price period.
                and not self._grid_charge_vetoed
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
            # The extension starts one price period after the last live step,
            # and the model must be anchored on that same instant.
            last_ts = price_start_times[-1] if price_start_times else now_utc
            extension_start = last_ts + timedelta(minutes=price_interval)
            resampled_prices = resampled_prices + self._model_extension(
                self._price_model, extension_start, steps_needed, price_interval
            )
            # Synthesise timestamps for the extension steps
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

        # Generate model forecast for price accuracy comparison. Always computed
        # when live prices are used, so users can compare prediction vs actual.
        # Projected onto the DP step windows, which is why it is built here and
        # not next to the horizon extension above.
        price_forecast_model: list[float] | None = None
        if self._price_model.has_data() and price_forecast_source.startswith("live"):
            price_forecast_model = self._model_series(
                self._price_model, step_starts, step_durations_hours
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
                # Own feed-in model has historical data — use it directly. The
                # feed-in series is already projected onto the DP step windows,
                # so the extension picks up at the first step it does not cover.
                resampled_feed_in.extend(
                    self._model_series(
                        self._feed_in_price_model,
                        step_starts[len(resampled_feed_in) :],
                        step_durations_hours[len(resampled_feed_in) :],
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
            feed_in_price_forecast_model = self._model_series(
                self._feed_in_price_model, step_starts, step_durations_hours
            )

        # Get current battery state
        battery_state = self.get_current_battery_state()

        # Cumulative throughput counters, read once: they close the previous
        # planned step and open the new one at the same instant.
        energy_totals_now = self._read_energy_totals()

        # Charge efficiency calibration: compare the previous plan against what
        # each battery actually moved, to detect systematic over-estimation of
        # charge efficiency (e.g. CV-phase).
        self._update_charge_eff_calibration(energy_totals_now)
        # Discharge efficiency calibration: same principle for discharging steps.
        self._update_discharge_eff_calibration(energy_totals_now)

        battery_config = self.battery_config

        # Calibration overrides replace the nominal curves for SoC TRANSITIONS
        # only; economic costs always use the nominal curves so a charging- or
        # discharging-speed problem is not double-counted as extra energy cost
        # or degradation. See efficiency_calibration.py for the two directions'
        # opposite arithmetic, and for the threshold below which a correction is
        # stored but not applied.
        charge_eff_correction = self.charge_eff_correction
        discharge_eff_correction = self.discharge_eff_correction
        charge_eff_curve_override = curve_override(
            battery_config.charge_efficiency_curve_parsed,
            charge_eff_correction,
        )
        discharge_eff_curve_override = curve_override(
            battery_config.discharge_efficiency_curve_parsed,
            discharge_eff_correction,
        )
        if charge_eff_curve_override is not None:
            _LOGGER.debug(
                "Charge efficiency correction %.3f applied to curve",
                charge_eff_correction,
            )
        if discharge_eff_curve_override is not None:
            _LOGGER.debug(
                "Discharge efficiency correction %.3f applied to curve",
                discharge_eff_correction,
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
        # Snapshot the counters and each pack's SoC alongside the plan they
        # belong to, so the next run can measure the throughput of exactly this
        # step, per battery. The setpoints themselves are snapshotted further
        # down, once the split is known.
        self._energy_counter_snapshot = energy_totals_now
        self._soc_snapshot_kwh = {
            sid: state.soc_kwh for sid, state in self._per_battery_states.items()
        }

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

        current_price = resampled_prices[0] if resampled_prices else 0.0
        effective_mode, effective_power = self._resolve_effective_mode(
            result,
            current_grid,
            resampled_feed_in,
            hybrid_deadband_w,
            battery_state.power_kw * 1000,
            current_price,
            degradation_cost_per_kwh,
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
            current_price=current_price,
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
        # Inputs the realtime loop needs to repeat the charge arbitration
        # between runs. _scheduled_charge_w is the setpoint to return to when
        # the surplus recovers: the committed power when this run followed the
        # schedule, the raw plan when it refused the import — whether it
        # downgraded to zero_grid/idle or only trimmed the charge to the
        # surplus. Storing the trimmed power there would latch it: the realtime
        # loop caps its own result with it, so the charge could never climb back
        # to the plan the sun can now cover.
        self._last_feed_in_price = self._current_feed_in_price(resampled_feed_in)
        self._last_grid_price = current_price
        self._last_degradation_cost_per_kwh = degradation_cost_per_kwh
        if result.optimal_mode == ACTION_CHARGING:
            self._scheduled_charge_w = (
                controller_schedule_w
                if effective_mode == ACTION_CHARGING and not self._grid_charge_vetoed
                else result.optimal_power_kw * 1000
            )
        else:
            self._scheduled_charge_w = 0.0

        # Calculate zero-grid control action using the resolved effective mode
        controller_mode = self._resolve_controller_mode(
            effective_mode, current_grid, battery_state.power_kw * 1000
        )

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
                # Surplus the hybrid branches judged, measured independently of
                # the battery's own action (see _net_surplus_w).
                "surplus_kw": round(
                    self._net_surplus_w(current_grid, battery_state.power_kw * 1000)
                    / 1000,
                    3,
                ),
                "grid_charge_vetoed": self._grid_charge_vetoed,
                "commitment_locked": commitment_locked,
                "commitment_reason": commitment_reason,
                "charge_eff_correction": round(self.charge_eff_correction, 4),
                "charge_eff_samples": self.charge_eff_sample_count,
                "charge_eff_last_result": self.charge_eff_last_result,
                "discharge_eff_correction": round(self.discharge_eff_correction, 4),
                "discharge_eff_samples": self.discharge_eff_sample_count,
                "discharge_eff_last_result": self.discharge_eff_last_result,
            }
        )
        self._optimization_trigger_source = "unknown"

        # Split combined setpoint across individual batteries
        combined_setpoint_kw = control_action["target_power_kw"]  # positive=charge
        battery_setpoints = self._split_setpoint(
            combined_setpoint_kw, self._control_mode
        )
        # Which pack was asked to do what, for the calibration that scores this
        # step on the next run. The dispatcher concentrates on one battery at a
        # time, so without this the fleet plan would be credited to every pack.
        self._last_battery_setpoints = dict(battery_setpoints)

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
            "charge_eff_correction": round(self.charge_eff_correction, 4),
            "charge_eff_samples": self.charge_eff_sample_count,
            "charge_eff_applied": self.charge_eff_applied,
            "charge_eff_last_result": self.charge_eff_last_result,
            "discharge_eff_correction": round(self.discharge_eff_correction, 4),
            "discharge_eff_samples": self.discharge_eff_sample_count,
            "discharge_eff_applied": self.discharge_eff_applied,
            "discharge_eff_last_result": self.discharge_eff_last_result,
            "battery_calibration": self.battery_calibration_report(),
            "timestamp": dt_util.utcnow(),
            "updated_at": dt_util.now().isoformat(),
        }

"""Forecast coordinator for the Battery Controller integration."""

from __future__ import annotations

import logging
from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta
from collections.abc import Iterable
from typing import Any

from collections import deque

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir, storage
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import (
    CONF_BATTERY_ENERGY_CHARGED_SENSOR,
    CONF_BATTERY_ENERGY_DISCHARGED_SENSOR,
    CONF_GRID_EXPORT_SENSORS,
    CONF_GRID_IMPORT_SENSORS,
    CONF_GROSS_LOAD_SENSORS,
    CONF_PV_FORECAST_SENSORS,
    CONF_PV_MEASURED_PRODUCTION_SENSOR,
    CONF_PV_PRODUCTION_SENSORS,
    DC_TO_AC_INVERTER_EFFICIENCY,
    DOMAIN,
    FORECAST_INTERVAL_MINUTES,
    PV_CALIBRATION_APPLY_MAX,
    PV_CALIBRATION_APPLY_MIN,
    PV_CALIBRATION_MAX_LOAD_FRACTION,
    PV_CALIBRATION_MAX_SAMPLE_RATIO,
    PV_CALIBRATION_MIN_LOAD_FRACTION,
    PV_CALIBRATION_MIN_SAMPLES,
    PV_CALIBRATION_WINDOW,
    WEATHER_STALE_AFTER_MINUTES,
)
from .coordinator_weather import WeatherDataCoordinator
from .forecast_models import (
    ConsumptionForecastModel,
    NetLoadForecast,
    PVForecastModel,
)
from .helpers import (
    battery_energy_sensor_ids,
    extract_pv_forecast_series,
    usable_state,
)

_LOGGER = logging.getLogger(__name__)

# What the last PV calibration attempt did per array. Published so a user can
# tell an array that is genuinely on target from one that has never had the
# chance to learn anything — most commonly because it has no measured
# production sensor, or because its output never lands in the usable band.
PV_CAL_SAMPLED = "sampled"
PV_CAL_NO_MEASURED_SENSOR = "no_measured_production_sensor"
PV_CAL_OUTSIDE_LOAD_BAND = "production_outside_usable_band"
PV_CAL_METER_UNREADABLE = "production_counter_unavailable"
PV_CAL_WINDOW_MISMATCH = "forecast_window_did_not_elapse"
PV_CAL_CURTAILED = "pv_curtailed"
PV_CAL_METER_RESET = "production_counter_reset"
PV_CAL_IMPLAUSIBLE = "sample_dropped_implausible"


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
        self.pv_ac_forecast_sensors: list[list[str]] = []
        self.pv_dc_models: list[PVForecastModel] = []
        self.pv_dc_subentry_ids: list[str] = []
        self.pv_dc_forecast_sensors: list[list[str]] = []
        for arr in config.get("pv_arrays", []):
            kwp = float(arr.get("peak_power_kwp", 0))
            if kwp <= 0:
                continue
            orientation = float(arr.get("orientation", 180.0))
            tilt = float(arr.get("tilt", 35.0))
            efficiency_factor = float(arr.get("efficiency_factor", 0.85))
            dc_coupled = bool(arr.get("dc_coupled", False))
            subentry_id = arr.get("subentry_id", "")
            # External PV forecast sensors (e.g. Solcast today/tomorrow):
            # when set, their data overrides the internal radiation model
            # for the hours they cover.
            forecast_sensors = list(arr.get(CONF_PV_FORECAST_SENSORS, []) or [])
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
                self.pv_dc_forecast_sensors.append(forecast_sensors)
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
                self.pv_ac_forecast_sensors.append(forecast_sensors)

        # Dummy zero-power PV model for NetLoadForecast (consumption only)
        _dummy_pv = PVForecastModel(
            peak_power_kwp=0.0, orientation_deg=180, tilt_deg=35, efficiency_factor=1.0
        )

        # Measured production per array, and the union used by the gross-load
        # reconstruction. The reconstruction only ever needs the sum, so the
        # legacy flat list keeps working untouched and the per-array sensors are
        # purely additive — no migration, and nothing to configure twice.
        self._pv_measured_sensors: dict[str, str] = {}
        for arr in config.get("pv_arrays", []):
            sid = arr.get("subentry_id", "")
            entity_id = arr.get(CONF_PV_MEASURED_PRODUCTION_SENSOR)
            if sid and entity_id:
                self._pv_measured_sensors[sid] = entity_id
        reconstruction_pv_sensors = list(config.get(CONF_PV_PRODUCTION_SENSORS, []))
        for entity_id in self._pv_measured_sensors.values():
            if entity_id not in reconstruction_pv_sensors:
                reconstruction_pv_sensors.append(entity_id)

        # Per-array forecast calibration state.
        self._pv_cal_samples: dict[str, deque[tuple[float, float]]] = {}
        self._pv_cal_correction: dict[str, float] = {}
        self._pv_cal_snapshot: dict[str, Any] = {}
        # Per array: why the most recent run did or did not take a sample.
        self._pv_cal_last_result: dict[str, str] = {}
        self._pv_cal_store: storage.Store[dict[str, Any]] = storage.Store(
            hass, 1, f"battery_controller_{config.get('entry_id', 'unknown')}_pv_cal"
        )
        # Set by the PV-curtailment switch (via the optimization coordinator):
        # while production is deliberately suppressed the shortfall against the
        # forecast is not a forecast error and must not be calibrated away.
        self.pv_curtailed: bool = False

        self.consumption_model = ConsumptionForecastModel(
            hass=hass,
            grid_import_sensors=config.get(CONF_GRID_IMPORT_SENSORS, []),
            grid_export_sensors=config.get(CONF_GRID_EXPORT_SENSORS, []),
            gross_load_sensors=config.get(CONF_GROSS_LOAD_SENSORS, []),
            history_days=14,
            base_consumption_kw=0.5,
            pv_production_sensors=reconstruction_pv_sensors,
            entry_id=config.get("entry_id"),
            battery_charge_sensors=battery_energy_sensor_ids(
                config.get("battery_subentries", []),
                CONF_BATTERY_ENERGY_CHARGED_SENSOR,
            ),
            battery_discharge_sensors=battery_energy_sensor_ids(
                config.get("battery_subentries", []),
                CONF_BATTERY_ENERGY_DISCHARGED_SENSOR,
            ),
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
        await self._async_load_pv_calibration()

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

    @property
    def pv_calibrated_array_ids(self) -> list[str]:
        """The arrays the calibration reports on: every configured PV array.

        Arrays with no measured production sensor are deliberately included.
        "There is nothing to learn from" is the most common reason a forecast
        is never corrected, and leaving those arrays out of the report is
        exactly what makes that invisible.
        """
        return [
            sid for sid in (*self.pv_ac_subentry_ids, *self.pv_dc_subentry_ids) if sid
        ]

    def pv_correction(self, subentry_id: str) -> float:
        """Learned gain factor applied to one PV array's forecast (1.0 = nominal)."""
        return self._pv_cal_correction.get(subentry_id, 1.0)

    def pv_sample_count(self, subentry_id: str) -> int:
        """Number of observations behind pv_correction for one array."""
        return len(self._pv_cal_samples.get(subentry_id, ()))

    def pv_correction_applied(self, subentry_id: str) -> bool:
        """Whether one array's forecast is currently being corrected.

        A correction is only derived once PV_CALIBRATION_MIN_SAMPLES
        observations exist, so this stays False while the window fills.
        """
        return subentry_id in self._pv_cal_correction

    def pv_last_result(self, subentry_id: str) -> str:
        """What the last calibration attempt for one array did, and why."""
        if subentry_id not in self._pv_measured_sensors:
            return PV_CAL_NO_MEASURED_SENSOR
        return self._pv_cal_last_result.get(subentry_id, PV_CAL_OUTSIDE_LOAD_BAND)

    async def _async_load_pv_calibration(self) -> None:
        """Restore per-array PV corrections from storage."""
        stored = await self._pv_cal_store.async_load()
        if not stored:
            return
        for sid, entry in (stored.get("arrays") or {}).items():
            samples = entry.get("samples") or []
            self._pv_cal_samples[sid] = deque(
                ((float(m), float(f)) for m, f in samples),
                maxlen=PV_CALIBRATION_WINDOW,
            )
            self._pv_cal_correction[sid] = float(entry.get("correction", 1.0))
        active = {
            sid: round(c, 3)
            for sid, c in self._pv_cal_correction.items()
            if abs(c - 1.0) > 0.01
        }
        if active:
            _LOGGER.info("Restored PV forecast calibration: %s", active)

    async def _async_save_pv_calibration(self) -> None:
        """Persist per-array PV corrections."""
        await self._pv_cal_store.async_save(
            {
                "arrays": {
                    sid: {
                        "samples": [[m, f] for m, f in samples],
                        "correction": self._pv_cal_correction.get(sid, 1.0),
                    }
                    for sid, samples in self._pv_cal_samples.items()
                }
            }
        )

    async def async_reset_pv_calibration(self) -> None:
        """Reset every per-array PV correction to the nominal 1.0."""
        if self._pv_cal_samples or any(
            abs(c - 1.0) > 1e-9 for c in self._pv_cal_correction.values()
        ):
            _LOGGER.info(
                "Resetting PV forecast calibration for %d array(s)",
                len(self._pv_cal_correction),
            )
        self._pv_cal_samples.clear()
        self._pv_cal_correction.clear()
        await self._async_save_pv_calibration()

    def _read_pv_meter_kwh(self, entity_id: str) -> float | None:
        """Read one cumulative production counter, in kWh."""
        state = usable_state(self.hass, entity_id)
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
        return value

    def _update_pv_calibration(self, now_utc: datetime) -> None:
        """Compare the previous step's forecast against what each array produced.

        The correction is an energy-weighted ratio, not a mean of per-step
        ratios: a quarter hour under cloud has a large relative error on a tiny
        amount of energy, and weighting by energy stops those steps dominating
        an estimate that is meant to describe a gain error.

        What it can and cannot fix is stated in const.py: a wrong tilt or
        orientation entry, soiling and a dead string are gain errors and are
        captured; shading is a function of sun position, not a constant factor,
        and is not.
        """
        snapshot = self._pv_cal_snapshot
        self._pv_cal_snapshot = {}
        if not snapshot:
            return

        planned_by_sid: dict[str, float] = snapshot["forecast_kwh"] or {}
        elapsed_min = (now_utc - snapshot["taken_at"]).total_seconds() / 60.0
        step_min = FORECAST_INTERVAL_MINUTES
        if not (0.5 * step_min <= elapsed_min <= 1.5 * step_min):
            # The window the forecast described is not the window that elapsed.
            self._set_pv_results(planned_by_sid, PV_CAL_WINDOW_MISMATCH)
            return

        if self.pv_curtailed:
            # Production was deliberately suppressed; the shortfall is not a
            # forecast error.
            self._set_pv_results(planned_by_sid, PV_CAL_CURTAILED)
            return

        changed = False
        for sid, planned_kwh in planned_by_sid.items():
            entity_id = self._pv_measured_sensors.get(sid)
            before = (snapshot["meter_kwh"] or {}).get(sid)
            if not entity_id or before is None or planned_kwh <= 0:
                self._pv_cal_last_result[sid] = PV_CAL_METER_UNREADABLE
                continue
            after = self._read_pv_meter_kwh(entity_id)
            if after is None:
                self._pv_cal_last_result[sid] = PV_CAL_METER_UNREADABLE
                continue
            measured_kwh = after - before
            if measured_kwh < 0:
                _LOGGER.debug(
                    "PV calibration: counter %s went backwards (%.3f -> %.3f kWh); "
                    "treating as a meter reset",
                    entity_id,
                    before,
                    after,
                )
                self._pv_cal_last_result[sid] = PV_CAL_METER_RESET
                continue
            if measured_kwh > PV_CALIBRATION_MAX_SAMPLE_RATIO * planned_kwh:
                _LOGGER.debug(
                    "PV calibration: dropping implausible sample for %s "
                    "(measured %.3f kWh vs forecast %.3f kWh)",
                    sid,
                    measured_kwh,
                    planned_kwh,
                )
                self._pv_cal_last_result[sid] = PV_CAL_IMPLAUSIBLE
                continue

            samples = self._pv_cal_samples.setdefault(
                sid, deque(maxlen=PV_CALIBRATION_WINDOW)
            )
            samples.append((measured_kwh, planned_kwh))
            self._pv_cal_last_result[sid] = PV_CAL_SAMPLED
            total_measured = sum(m for m, _ in samples)
            total_planned = sum(f for _, f in samples)
            if len(samples) < PV_CALIBRATION_MIN_SAMPLES or total_planned <= 0:
                continue
            correction = max(
                PV_CALIBRATION_APPLY_MIN,
                min(PV_CALIBRATION_APPLY_MAX, total_measured / total_planned),
            )
            previous = self._pv_cal_correction.get(sid, 1.0)
            self._pv_cal_correction[sid] = correction
            if abs(correction - previous) > 0.005:
                changed = True
                _LOGGER.info(
                    "PV forecast correction for array %s: %.3f → %.3f "
                    "(n=%d steps, measured %.2f kWh vs forecast %.2f kWh)",
                    sid,
                    previous,
                    correction,
                    len(samples),
                    total_measured,
                    total_planned,
                )
        if changed:
            self.hass.async_create_task(self._async_save_pv_calibration())

    def _set_pv_results(self, sids: Iterable[str], result: str) -> None:
        """Record the same calibration outcome for several arrays at once."""
        for sid in sids:
            self._pv_cal_last_result[sid] = result

    def _snapshot_pv_calibration(
        self,
        now_utc: datetime,
        raw_first_step_kw: dict[str, float],
        kwp_by_sid: dict[str, float],
    ) -> None:
        """Record what the next step is expected to produce, and the meters now.

        Only arrays whose forecast falls in the middle of their rating are
        recorded: below the floor the signal is smaller than the noise, above
        the ceiling inverter clipping and thermal derating dominate and are not
        forecast errors.
        """
        step_hours = FORECAST_INTERVAL_MINUTES / 60.0
        forecast_kwh: dict[str, float] = {}
        meter_kwh: dict[str, float] = {}
        for sid, entity_id in self._pv_measured_sensors.items():
            kwp = kwp_by_sid.get(sid, 0.0)
            raw_kw = raw_first_step_kw.get(sid, 0.0)
            if kwp <= 0:
                self._pv_cal_last_result[sid] = PV_CAL_OUTSIDE_LOAD_BAND
                continue
            load_fraction = raw_kw / kwp
            if not (
                PV_CALIBRATION_MIN_LOAD_FRACTION
                <= load_fraction
                <= PV_CALIBRATION_MAX_LOAD_FRACTION
            ):
                # Night, deep cloud or near-clipping output: no usable signal.
                self._pv_cal_last_result[sid] = PV_CAL_OUTSIDE_LOAD_BAND
                continue
            meter = self._read_pv_meter_kwh(entity_id)
            if meter is None:
                self._pv_cal_last_result[sid] = PV_CAL_METER_UNREADABLE
                continue
            forecast_kwh[sid] = raw_kw * step_hours
            meter_kwh[sid] = meter
        if forecast_kwh:
            self._pv_cal_snapshot = {
                "taken_at": now_utc,
                "forecast_kwh": forecast_kwh,
                "meter_kwh": meter_kwh,
            }

    def _apply_sensor_forecast(
        self,
        model_forecast: list[float],
        forecast_sensors: list[str],
        timestamps_utc: list[datetime],
    ) -> list[float]:
        """Override the model forecast with external PV forecast sensor data.

        Reads the forecast series from the configured sensors (e.g. the
        Solcast integration's today/tomorrow sensors) at the source's native
        resolution. Sources finer than the forecast step (e.g. Volcast's
        5-minute data) are averaged within each step; coarser sources (30- or
        60-minute periods) map each step onto the sensor period covering it —
        a period runs until the next entry's start, capped at one hour — so
        30-minute Solcast data lands on two 15-minute steps without loss.
        Steps outside the sensor horizon keep the internal model forecast.
        When the sensors yield no usable data at all, the internal model
        forecast is returned unchanged.
        """
        if not forecast_sensors:
            return model_forecast
        states = [self.hass.states.get(entity_id) for entity_id in forecast_sensors]
        series = extract_pv_forecast_series([s for s in states if s is not None])
        if not series:
            _LOGGER.warning(
                "No usable PV forecast data from sensor(s) %s; "
                "falling back to internal radiation model",
                forecast_sensors,
            )
            return model_forecast
        starts = [entry_ts for entry_ts, _ in series]
        max_period = timedelta(hours=1)
        step_delta = (
            timestamps_utc[1] - timestamps_utc[0]
            if len(timestamps_utc) > 1
            else timedelta(minutes=FORECAST_INTERVAL_MINUTES)
        )
        result: list[float] = []
        overridden = 0
        for i, ts in enumerate(timestamps_utc):
            value: float | None = None
            # Sub-step source: average all entries starting within this step
            lo = bisect_left(starts, ts)
            hi = bisect_left(starts, ts + step_delta)
            if hi - lo > 1:
                value = sum(v for _, v in series[lo:hi]) / (hi - lo)
            else:
                # Step-or-coarser source: value of the period covering ts
                idx = bisect_right(starts, ts) - 1
                if idx >= 0:
                    period_start = starts[idx]
                    if idx + 1 < len(starts):
                        period_end = starts[idx + 1]
                    elif idx > 0:
                        # Last entry: assume the same length as the previous
                        # period so fine-grained sources don't extend an hour
                        period_end = period_start + (starts[idx] - starts[idx - 1])
                    else:
                        period_end = period_start + max_period
                    period_end = min(period_end, period_start + max_period)
                    if ts < period_end:
                        value = series[idx][1]
            if value is None:
                value = model_forecast[i] if i < len(model_forecast) else 0.0
            else:
                overridden += 1
            result.append(max(0.0, value))
        _LOGGER.debug(
            "PV forecast from %s: %d of %d steps from sensor data",
            forecast_sensors,
            overridden,
            len(timestamps_utc),
        )
        return result

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

        # Fold in what the previous step's forecast turned out to be worth,
        # before this run's forecast is corrected with the result.
        self._update_pv_calibration(now_utc)
        raw_first_step_kw: dict[str, float] = {}
        kwp_by_sid: dict[str, float] = {}

        per_pv_array_forecasts: dict[str, list[float]] = {}

        def _sum_arrays(
            subentry_ids: list[str],
            models: list[PVForecastModel],
            sensors_per_array: list[list[str]],
        ) -> list[float]:
            """Forecast one coupling group (AC or DC) and sum its arrays.

            Per-array series are recorded for the diagnostic sensors and for
            calibration on the way through.
            """
            total = [0.0] * n
            for sid, model, sensors in zip(subentry_ids, models, sensors_per_array):
                series = model.forecast_from_radiation(
                    radiation_steps,
                    temp_for_pv,
                    dni_forecast=poa_dni,
                    diffuse_forecast=poa_diffuse,
                    timestamps_utc=timestamps_utc,
                    latitude=lat,
                    longitude=lon,
                )
                series = self._apply_sensor_forecast(series, sensors, timestamps_utc)
                if sid:
                    # Snapshot the UNcorrected value: the factor is always
                    # measured/raw, so it stays a direct estimate instead of
                    # compounding on itself run after run.
                    raw_first_step_kw[sid] = series[0] if series else 0.0
                    kwp_by_sid[sid] = model.peak_power_kwp
                gain = self.pv_correction(sid)
                if gain != 1.0:
                    series = [v * gain for v in series]
                if sid:
                    per_pv_array_forecasts[sid] = [
                        round(max(0.0, v), 3) for v in series[:n]
                    ]
                for i in range(min(n, len(series))):
                    total[i] += series[i]
            # Clamp: a faulty sensor/model must not produce negative output (P1.3)
            return [max(0.0, v) for v in total]

        pv_forecast = _sum_arrays(
            self.pv_ac_subentry_ids, self.pv_ac_models, self.pv_ac_forecast_sensors
        )
        has_dc = bool(self.pv_dc_models)
        pv_dc_forecast = _sum_arrays(
            self.pv_dc_subentry_ids, self.pv_dc_models, self.pv_dc_forecast_sensors
        )

        # Net load = consumption minus all PV that reaches the AC side.
        # DC-coupled PV gets there through the inverter, the same path the
        # optimizer's baseline uses (_calculate_baseline_cost). Counting only
        # the AC series reported a DC-coupled system as importing its entire
        # household load while the sun was in fact covering it — the AC series
        # is legitimately zero when every array is DC-coupled.
        total_pv_forecast = [
            p + dc * DC_TO_AC_INVERTER_EFFICIENCY
            for p, dc in zip(pv_forecast, pv_dc_forecast)
        ]
        net_load_forecast = [
            c - p for c, p in zip(consumption_forecast, total_pv_forecast)
        ]

        # Derive current values from forecast (first element = current step)
        current_pv = pv_forecast[0] if pv_forecast else 0.0
        current_dc_pv = pv_dc_forecast[0] if pv_dc_forecast else 0.0
        current_total_pv = current_pv + current_dc_pv * DC_TO_AC_INVERTER_EFFICIENCY
        current_consumption = self.consumption_model.get_current_consumption()

        # Record what the step about to elapse should produce, so the next run
        # can score it.
        self._snapshot_pv_calibration(now_utc, raw_first_step_kw, kwp_by_sid)

        result = {
            "pv_forecast_kw": [round(v, 3) for v in pv_forecast],
            "pv_dc_forecast_kw": [round(v, 3) for v in pv_dc_forecast],
            "per_pv_array_forecasts": per_pv_array_forecasts,
            "consumption_forecast_kw": [round(v, 3) for v in consumption_forecast],
            "net_load_forecast_kw": [round(v, 3) for v in net_load_forecast],
            "current_pv_kw": round(current_pv, 3),
            "current_dc_pv_kw": round(current_dc_pv, 3),
            "current_consumption_kw": round(current_consumption, 3),
            "current_net_load_kw": round(current_consumption - current_total_pv, 3),
            "current_ghi_wm2": round(radiation_steps[0], 1) if radiation_steps else 0.0,
            "current_wind_speed_ms": round(wind_speed_steps[0], 1)
            if wind_speed_steps
            else 0.0,
            "pv_dc_coupled": has_dc,
            "pv_calibration": {
                sid: {
                    "correction": round(self.pv_correction(sid), 4),
                    "samples": self.pv_sample_count(sid),
                    "applied": self.pv_correction_applied(sid),
                    "last_result": self.pv_last_result(sid),
                }
                for sid in self.pv_calibrated_array_ids
            },
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

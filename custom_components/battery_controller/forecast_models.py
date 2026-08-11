"""Forecast models for PV production and consumption."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import (
    DEFAULT_PV_ORIENTATION_DEG,
    DEFAULT_PV_TILT_DEG,
    MAX_PLAUSIBLE_CONSUMPTION_KW,
)
from .helpers import (
    calculate_consumption_pattern,
    calculate_pv_forecast,
    price_unit_scale_from_state,
)

_LOGGER = logging.getLogger(__name__)


def _get_season(month: int) -> int:
    """Return meteorological season index for a given month.

    0 = Winter (DJF: Dec, Jan, Feb)
    1 = Spring (MAM: Mar, Apr, May)
    2 = Summer (JJA: Jun, Jul, Aug)
    3 = Autumn (SON: Sep, Oct, Nov)
    """
    return {12: 0, 1: 0, 2: 0, 3: 1, 4: 1, 5: 1, 6: 2, 7: 2, 8: 2, 9: 3, 10: 3, 11: 3}[
        month
    ]


class PVForecastModel:
    """Model for PV production forecasting."""

    # Temperature coefficient for standard silicon PV panels: ~0.4%/°C above 25°C.
    # Applied when temperature_forecast is provided to forecast_from_radiation().
    _TEMP_COEFF_PER_C = 0.004
    _TEMP_STC_C = 25.0  # Standard Test Condition temperature

    def __init__(
        self,
        peak_power_kwp: float = 0.0,
        orientation_deg: float = DEFAULT_PV_ORIENTATION_DEG,
        tilt_deg: float = DEFAULT_PV_TILT_DEG,
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
        temperature_forecast: list[float] | None = None,
        dni_forecast: list[float] | None = None,
        diffuse_forecast: list[float] | None = None,
        timestamps_utc: list[datetime] | None = None,
        latitude: float | None = None,
        longitude: float | None = None,
    ) -> list[float]:
        """Generate PV forecast from solar radiation data.

        When dni_forecast, diffuse_forecast, timestamps_utc, latitude, and longitude
        are provided, uses a proper POA transposition model (see calculate_pv_forecast).
        Falls back to a simplified GHI-based model when any are missing.

        Applies panel temperature derating when temperature_forecast is provided.
        Standard silicon panels lose ~0.4%/°C above 25°C (STC). On a 35°C day
        this reduces output by ~4% compared to a radiation-only estimate.

        Args:
            radiation_forecast: GHI forecast in W/m²
            temperature_forecast: Ambient temperature in °C (optional).
            dni_forecast: Direct Normal Irradiance in W/m² (optional).
            diffuse_forecast: Diffuse Horizontal Irradiance in W/m² (optional).
            timestamps_utc: UTC datetime for each forecast hour (optional).
            latitude: Site latitude in degrees (optional).
            longitude: Site longitude in degrees (optional).

        Returns:
            PV production forecast in kW
        """
        base_forecast = calculate_pv_forecast(
            radiation_forecast,
            self.peak_power_kwp,
            self.orientation_deg,
            self.tilt_deg,
            self.efficiency_factor,
            dni_forecast=dni_forecast,
            diffuse_forecast=diffuse_forecast,
            timestamps_utc=timestamps_utc,
            latitude=latitude,
            longitude=longitude,
        )

        if temperature_forecast is None:
            return base_forecast

        # Apply temperature derating: max 20% reduction floor (very hot panels)
        result = []
        for i, pv_kw in enumerate(base_forecast):
            if i < len(temperature_forecast):
                temp_c = temperature_forecast[i]
                delta = max(0.0, temp_c - self._TEMP_STC_C)
                derating = max(0.80, 1.0 - self._TEMP_COEFF_PER_C * delta)
                result.append(pv_kw * derating)
            else:
                result.append(pv_kw)
        return result


class ConsumptionForecastModel:
    """Model for household consumption forecasting.

    Uses DSMR-style energy sensors (kWh, total_increasing) for pattern learning.
    Hourly kWh change from HA statistics equals average kW during that hour.

    What the pattern must represent is gross household load: everything the house
    draws, whatever served it. There are two ways to arrive at that, and which one
    applies is explicit rather than inferred from which fields happen to be set.

    **Measured** — gross_load_sensors is set. A meter between the inverter and the
    house reports the figure directly, so it is used as-is and no correction of any
    kind applies. More accurate than summing several meters, and the only workable
    source when the component set is incomplete (e.g. DC-coupled PV with no DC
    counter, where the reconstruction would silently under-report).

    **Reconstructed** — otherwise, from the physical measurements:

        gross = import - export + pv + discharge - charge

    grid_import_sensors and grid_export_sensors give the first two. For PV there
    are three layers, first available wins:
    1. pv_production_sensors: real kWh sensors from inverter(s)
    2. own pv_forecast sensor history (via entry_id lookup) → self-consistent
    3. Warning log when neither is available
    Battery charge and discharge come from the per-subentry energy counters; a
    warning is logged when they are missing, since grid charging would otherwise
    be learned as household load.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        grid_import_sensors: list[str] | None = None,
        grid_export_sensors: list[str] | None = None,
        history_days: int = 14,
        base_consumption_kw: float = 0.5,
        pv_production_sensors: list[str] | None = None,
        entry_id: str | None = None,
        battery_charge_sensors: list[str] | None = None,
        battery_discharge_sensors: list[str] | None = None,
        gross_load_sensors: list[str] | None = None,
    ):
        """Initialize consumption forecast model."""
        self.hass = hass
        self.grid_import_sensors = grid_import_sensors or []
        self.grid_export_sensors = grid_export_sensors or []
        self.gross_load_sensors = gross_load_sensors or []
        self.history_days = history_days
        self.base_consumption_kw = base_consumption_kw
        self.pv_production_sensors = pv_production_sensors or []
        self.battery_charge_sensors = battery_charge_sensors or []
        self.battery_discharge_sensors = battery_discharge_sensors or []
        self._entry_id = entry_id
        # Non-seasonal pattern (hour, day_of_week) → average kW
        self._hourly_pattern: dict[tuple[int, int], float] = {}
        # Seasonal pattern (hour, day_of_week, season) → average kW
        # season: 0=winter(DJF), 1=spring(MAM), 2=summer(JJA), 3=autumn(SON)
        # Requires at least 2 samples per bucket to be used (same threshold as hourly).
        self._seasonal_pattern: dict[tuple[int, int, int], float] = {}
        # Minimum samples to trust a seasonal bucket
        self._SEASONAL_MIN_SAMPLES: int = 2

    async def async_update_pattern(self) -> None:
        """Update consumption pattern from historical energy data.

        Queries HA recorder statistics for hourly energy changes (kWh). kWh per
        hour equals average kW, so values map directly to power.

        Measured mode (gross_load_sensors set) takes the figure as-is.
        Otherwise it is reconstructed as import - export + pv + discharge - charge;
        see the class docstring for the PV fallback layers.
        """
        measured = bool(self.gross_load_sensors)
        if measured:
            all_sensors = list(self.gross_load_sensors)
        else:
            all_sensors = self.grid_import_sensors + self.grid_export_sensors
        if not all_sensors:
            return

        try:
            from homeassistant.components.recorder.util import get_instance  # type: ignore[attr-defined, unused-ignore]
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
            hourly_net: dict[datetime, float] = {}  # key: UTC hour -> net kWh

            # Normalise a stat entry to (start_dt, value).  The recorder returns
            # "start" as a Unix timestamp (float) on current Home Assistant
            # versions, and as a datetime or ISO string on older ones — all
            # three must be handled.  Keying on the datetime itself avoids a
            # string round-trip: formatting a float as "1753617600.0" and
            # parsing it back yields None, which silently dropped every bucket
            # and left the learned pattern empty.
            def _ts_and_value(stat: Any, field: str) -> tuple[datetime, float] | None:
                value = stat.get(field)
                if value is None:
                    return None
                start = stat.get("start")
                if isinstance(start, datetime):
                    start_dt = start
                elif isinstance(start, (int, float)):
                    start_dt = datetime.fromtimestamp(start, tz=UTC)
                else:
                    parsed = dt_util.parse_datetime(str(start or ""))
                    if parsed is None:
                        return None
                    start_dt = parsed
                return start_dt, float(value)

            positive = self.gross_load_sensors if measured else self.grid_import_sensors
            for sensor_id in positive:
                for stat in stats.get(sensor_id, []):
                    result = _ts_and_value(stat, "change")
                    if result:
                        stat_dt, val = result
                        hourly_net[stat_dt] = hourly_net.get(stat_dt, 0.0) + val

            if not measured:
                for sensor_id in self.grid_export_sensors:
                    for stat in stats.get(sensor_id, []):
                        result = _ts_and_value(stat, "change")
                        if result:
                            stat_dt, val = result
                            hourly_net[stat_dt] = hourly_net.get(stat_dt, 0.0) - val

            # PV correction: add back historical PV production so that the
            # stored pattern represents gross household consumption.
            # This prevents double-counting when the optimizer subtracts pv_forecast.
            pv_corrected = False
            if not measured and self.pv_production_sensors:
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
                            stat_dt, val = result
                            hourly_net[stat_dt] = hourly_net.get(stat_dt, 0.0) + max(
                                0.0, val
                            )
                pv_corrected = True
                _LOGGER.debug(
                    "PV correction applied from %d production sensor(s)",
                    len(self.pv_production_sensors),
                )
            elif not measured and self._entry_id:
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
                                stat_dt, val = result
                                # mean kW over 1 h = kWh for that hour
                                hourly_net[stat_dt] = hourly_net.get(
                                    stat_dt, 0.0
                                ) + max(0.0, val)
                        pv_corrected = True
                        _LOGGER.debug(
                            "PV correction applied from own pv_forecast sensor (%s)",
                            pv_entity_id,
                        )
                except Exception as err:
                    _LOGGER.debug(
                        "Could not apply PV correction from forecast sensor: %s", err
                    )

            if not measured and not pv_corrected:
                # Layer 3: warn that double-counting may occur
                _LOGGER.warning(
                    "Household load is reconstructed from grid import and export, but "
                    "no PV correction could be applied. PV that served the house is "
                    "then missing from the pattern. Configure 'pv_production_sensors' "
                    "with your inverter's total energy sensor(s) to fix this."
                )

            # Battery correction: complete the identity
            #   gross = import - export + pv + discharge - charge
            # Charging from the grid passes the grid meter, so without this it is
            # reconstructed as household load; discharging displaces import, so
            # without this the load it served goes missing. Both errors are
            # concentrated in specific hours, which is exactly the axis the
            # pattern is learned on.
            battery_ids = set(self.battery_charge_sensors) | set(
                self.battery_discharge_sensors
            )
            if not measured and battery_ids:
                battery_stats = await get_instance(self.hass).async_add_executor_job(
                    statistics_during_period,
                    self.hass,
                    start_time,
                    end_time,
                    battery_ids,
                    "hour",
                    None,
                    {"change"},
                )
                # Both are non-negative cumulative counters; clamp so a bogus
                # negative change cannot flip the correction's direction.
                for sensor_id in self.battery_discharge_sensors:
                    for stat in battery_stats.get(sensor_id, []):
                        result = _ts_and_value(stat, "change")
                        if result:
                            stat_dt, val = result
                            hourly_net[stat_dt] = hourly_net.get(stat_dt, 0.0) + max(
                                0.0, val
                            )
                for sensor_id in self.battery_charge_sensors:
                    for stat in battery_stats.get(sensor_id, []):
                        result = _ts_and_value(stat, "change")
                        if result:
                            stat_dt, val = result
                            hourly_net[stat_dt] = hourly_net.get(stat_dt, 0.0) - max(
                                0.0, val
                            )
                _LOGGER.debug(
                    "Battery correction applied from %d charge and %d discharge sensor(s)",
                    len(self.battery_charge_sensors),
                    len(self.battery_discharge_sensors),
                )
            elif not measured:
                _LOGGER.warning(
                    "Household load is reconstructed from grid import and export, but "
                    "no battery energy counters are set. Grid charging will be learned "
                    "as household consumption. Set the charged and discharged sensors "
                    "on each battery to fix this."
                )

            # Group by (hour, day_of_week) and also (hour, day_of_week, season).
            # Convert to local time so the pattern aligns with the local-time
            # forecast generated in ConsumptionForecastModel.forecast().
            # season: 0=winter(DJF), 1=spring(MAM), 2=summer(JJA), 3=autumn(SON)
            hourly_values: dict[tuple[int, int], list[float]] = {}
            seasonal_values: dict[tuple[int, int, int], list[float]] = {}
            dropped_outliers = 0
            for stat_dt, net_kwh in hourly_net.items():
                dt_local = dt_util.as_local(stat_dt)
                val = max(0.0, net_kwh)
                # Reject meter artefacts. An hourly "change" equals the average
                # power over that hour, so a value above the plausibility
                # ceiling cannot be household load — it is a sensor that jumped,
                # was replaced, or briefly reported nonsense. Such a sample is
                # unbounded, and with only ~2 samples per (hour, weekday) bucket
                # over 14 days a single one dominates the average and propagates
                # into the DP cost.
                if val > MAX_PLAUSIBLE_CONSUMPTION_KW:
                    dropped_outliers += 1
                    continue
                key = (dt_local.hour, dt_local.weekday())
                hourly_values.setdefault(key, []).append(val)
                season = _get_season(dt_local.month)
                seasonal_values.setdefault(
                    (dt_local.hour, dt_local.weekday(), season), []
                ).append(val)

            if dropped_outliers:
                _LOGGER.warning(
                    "Ignored %d implausible hourly sample(s) above %.0f kW while "
                    "learning the consumption pattern. This points at a meter "
                    "artefact in one of the configured energy sensors (a "
                    "total_increasing sensor that jumped or was replaced). Check "
                    "the statistics of your consumption/production/PV sensors "
                    "around the affected hours in Developer Tools -> Statistics.",
                    dropped_outliers,
                    MAX_PLAUSIBLE_CONSUMPTION_KW,
                )

            for h_key, values in hourly_values.items():
                if values:
                    self._hourly_pattern[h_key] = sum(values) / len(values)

            for s_key, values in seasonal_values.items():
                if len(values) >= self._SEASONAL_MIN_SAMPLES:
                    self._seasonal_pattern[s_key] = sum(values) / len(values)

            _LOGGER.debug(
                "Updated consumption pattern from %d energy sensors, "
                "%d hourly buckets, %d seasonal buckets",
                len(all_sensors),
                len(self._hourly_pattern),
                len(self._seasonal_pattern),
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
            season = _get_season(dt.month)

            # Priority: seasonal pattern → hourly pattern → default
            seasonal_key = (hour, dow, season)
            if seasonal_key in self._seasonal_pattern:
                forecast.append(self._seasonal_pattern[seasonal_key])
            elif (hour, dow) in self._hourly_pattern:
                forecast.append(self._hourly_pattern[(hour, dow)])
            else:
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
        season = _get_season(now.month)
        seasonal_key = (now.hour, now.weekday(), season)
        if seasonal_key in self._seasonal_pattern:
            return self._seasonal_pattern[seasonal_key]
        if (now.hour, now.weekday()) in self._hourly_pattern:
            return self._hourly_pattern[(now.hour, now.weekday())]
        return calculate_consumption_pattern(
            now.hour, now.weekday(), self.base_consumption_kw
        )


class PriceForecastModel:
    """Price forecast model using historical patterns with optional weather correction.

    Falls back gracefully when data is sparse:
    1. (hour, day_of_week, ghi_bin, wind_bin) → average price  [weather-corrected]
    2. (hour, day_of_week) → average price                     [simple pattern]
    3. Overall average price                                    [last resort]

    The model improves over time as more historical data accumulates in HA recorder.
    Weather sensor data (GHI, wind speed) is only available after the integration
    starts logging those sensors.
    """

    # W/m² boundaries → bins: dark/night, overcast, partial cloud, bright sun
    _GHI_THRESHOLDS = (50.0, 200.0, 500.0)
    # m/s boundaries → bins: calm, moderate, strong wind
    _WIND_THRESHOLDS = (4.0, 8.0)
    # Minimum samples required to use a bin (avoids noise from sparse data)
    _MIN_SAMPLES = 2
    # Amplification factor for std deviation: sharpens peaks/valleys relative to overall avg
    _STD_AMPLIFICATION: float = 0.5

    def __init__(
        self,
        hass: HomeAssistant,
        price_sensor_id: str,
        entry_id: str | None = None,
        history_days: int = 7,
    ) -> None:
        """Initialize price forecast model."""
        self.hass = hass
        self.price_sensor_id = price_sensor_id
        self._entry_id = entry_id
        self.history_days = history_days
        self._weather_pattern: dict[tuple[int, int, int, int], list[float]] = {}
        self._simple_pattern: dict[tuple[int, int], list[float]] = {}
        self._overall_avg: float | None = None

    @classmethod
    def _ghi_bin(cls, ghi: float) -> int:
        """Return bin index for a GHI value (W/m²)."""
        for i, threshold in enumerate(cls._GHI_THRESHOLDS):
            if ghi < threshold:
                return i
        return len(cls._GHI_THRESHOLDS)

    @classmethod
    def _wind_bin(cls, wind: float) -> int:
        """Return bin index for a wind speed value (m/s)."""
        for i, threshold in enumerate(cls._WIND_THRESHOLDS):
            if wind < threshold:
                return i
        return len(cls._WIND_THRESHOLDS)

    def has_data(self) -> bool:
        """Return True if the model has enough data for a useful forecast."""
        return self._overall_avg is not None

    async def async_update_pattern(self) -> None:
        """Update price pattern from historical recorder data."""
        if not self.price_sensor_id:
            return

        try:
            from homeassistant.components.recorder.util import get_instance  # type: ignore[attr-defined, unused-ignore]
            from homeassistant.components.recorder.statistics import (
                statistics_during_period,
            )

            end_time = dt_util.utcnow()
            start_time = end_time - timedelta(days=self.history_days)

            # Resolve GHI and wind sensor entity IDs via entity registry
            ghi_entity_id: str | None = None
            wind_entity_id: str | None = None
            if self._entry_id:
                try:
                    from homeassistant.helpers import entity_registry as er
                    from .const import DOMAIN

                    ent_reg = er.async_get(self.hass)
                    ghi_entity_id = ent_reg.async_get_entity_id(
                        "sensor", DOMAIN, f"{self._entry_id}_ghi"
                    )
                    wind_entity_id = ent_reg.async_get_entity_id(
                        "sensor", DOMAIN, f"{self._entry_id}_wind_speed_ms"
                    )
                except Exception as err:
                    _LOGGER.debug("Could not resolve weather sensor IDs: %s", err)

            # Query price sensor statistics (hourly mean)
            price_stats = await get_instance(self.hass).async_add_executor_job(
                statistics_during_period,
                self.hass,
                start_time,
                end_time,
                {self.price_sensor_id},
                "hour",
                None,
                {"mean"},
            )

            def _stat_dt(stat: Any) -> datetime | None:
                """Extract start datetime directly from a statistics dict entry."""
                start = stat.get("start")
                if start is None:
                    return None
                if isinstance(start, datetime):
                    return start
                # Fallback: try parsing string or Unix timestamp
                if isinstance(start, (int, float)):
                    from datetime import timezone

                    return datetime.fromtimestamp(start, tz=timezone.utc)
                parsed = dt_util.parse_datetime(str(start))
                return parsed

            # price_hourly stores (datetime, price) to avoid string round-trip issues
            price_hourly: list[tuple[datetime, float]] = []
            for stat in price_stats.get(self.price_sensor_id, []):
                mean = stat.get("mean")
                dt_obj = _stat_dt(stat)
                if mean is not None and dt_obj is not None:
                    price_hourly.append((dt_obj, float(mean)))

            if not price_hourly:
                _LOGGER.info(
                    "Price model: no statistics found for '%s' "
                    "(query returned keys: %s); price_forecast_predicted unavailable",
                    self.price_sensor_id,
                    list(price_stats.keys())[:5] if price_stats else "none",
                )
                return

            # Recorder statistics are in the sensor's native unit. Sensors
            # publishing €/MWh (e.g. OMIE) must be scaled to EUR/kWh so the
            # learned pattern matches the live forecast fed to the optimizer.
            unit_scale = price_unit_scale_from_state(
                self.hass.states.get(self.price_sensor_id)
            )
            if unit_scale != 1.0:
                price_hourly = [(dt, p * unit_scale) for dt, p in price_hourly]
                _LOGGER.debug(
                    "Price model: scaling recorder prices by %s (sensor unit per MWh)",
                    unit_scale,
                )

            # Query weather sensor statistics if GHI/wind sensors exist
            # Key: UTC datetime rounded to hour → value for bin lookup
            ghi_hourly: dict[datetime, float] = {}
            wind_hourly: dict[datetime, float] = {}
            weather_ids = {sid for sid in (ghi_entity_id, wind_entity_id) if sid}
            if weather_ids:
                weather_stats = await get_instance(self.hass).async_add_executor_job(
                    statistics_during_period,
                    self.hass,
                    start_time,
                    end_time,
                    weather_ids,
                    "hour",
                    None,
                    {"mean"},
                )
                for stat in weather_stats.get(ghi_entity_id or "", []):
                    mean = stat.get("mean")
                    dt_obj = _stat_dt(stat)
                    if mean is not None and dt_obj is not None:
                        ghi_hourly[dt_obj] = float(mean)
                for stat in weather_stats.get(wind_entity_id or "", []):
                    mean = stat.get("mean")
                    dt_obj = _stat_dt(stat)
                    if mean is not None and dt_obj is not None:
                        wind_hourly[dt_obj] = float(mean)

            # Build lookup tables
            weather_raw: dict[tuple[int, int, int, int], list[float]] = {}
            simple_raw: dict[tuple[int, int], list[float]] = {}
            all_prices: list[float] = []

            for dt_obj, price in price_hourly:
                # Convert to local time so the pattern aligns with the local-time
                # forecast generated in PriceForecastModel.forecast().
                dt_local = dt_util.as_local(dt_obj)
                hour = dt_local.hour
                dow = dt_local.weekday()
                all_prices.append(price)
                simple_raw.setdefault((hour, dow), []).append(price)
                if dt_obj in ghi_hourly and dt_obj in wind_hourly:
                    gb = self._ghi_bin(ghi_hourly[dt_obj])
                    wb = self._wind_bin(wind_hourly[dt_obj])
                    weather_raw.setdefault((hour, dow, gb, wb), []).append(price)

            self._simple_pattern = simple_raw
            self._weather_pattern = weather_raw
            self._overall_avg = (
                sum(all_prices) / len(all_prices) if all_prices else None
            )

            weather_bins_ok = sum(
                1 for v in weather_raw.values() if len(v) >= self._MIN_SAMPLES
            )
            simple_bins_ok = sum(
                1 for v in simple_raw.values() if len(v) >= self._MIN_SAMPLES
            )
            _LOGGER.debug(
                "Price model updated: %d price hours, %d/%d simple bins, "
                "%d/%d weather bins usable (min %d samples)",
                len(price_hourly),
                simple_bins_ok,
                len(simple_raw),
                weather_bins_ok,
                len(weather_raw),
                self._MIN_SAMPLES,
            )

        except ImportError:
            _LOGGER.debug("Recorder not available for price pattern learning")
        except Exception as err:
            _LOGGER.warning("Failed to update price pattern: %s", err)

    def forecast(
        self,
        hours: int = 24,
        start_time: datetime | None = None,
        ghi_forecast: list[float] | None = None,
        wind_forecast: list[float] | None = None,
    ) -> list[float]:
        """Generate hourly price forecast from historical patterns.

        Lookup priority per hour:
        1. (hour, dow, ghi_bin, wind_bin) when weather data available and bins populated
        2. (hour, dow) simple historical average
        3. Overall average price across all recorded hours

        Each bin price is sharpened with std deviation: avg ± _STD_AMPLIFICATION × std,
        where direction follows whether the bin avg is above or below the overall average.
        This restores peak/valley structure that plain averaging flattens out.

        Args:
            hours: Number of hours to forecast.
            start_time: Start time (default: current hour, rounded down).
            ghi_forecast: Solar irradiance forecast in W/m² per hour.
            wind_forecast: Wind speed forecast in m/s per hour.

        Returns:
            Hourly price forecast list in EUR/kWh.
        """
        if start_time is None:
            start_time = dt_util.now().replace(minute=0, second=0, microsecond=0)

        overall = self._overall_avg or 0.20

        def _sharpen(vals: list[float]) -> float:
            """Return avg ± k×std, direction based on deviation from overall avg.

            The amplification is shrunk by (n-1)/n. A bin only needs
            _MIN_SAMPLES (2) observations to be used, and at n = 2 the spread of
            two points is not evidence of a peak — it is the difference between
            two draws. Shrinking towards the plain average there, and relaxing
            to the full amplification as samples accumulate, keeps the
            peak/valley structure the sharpening exists for without letting two
            noisy hours invent one. The sample standard deviation (n-1
            denominator) is used for the same reason.
            """
            n = len(vals)
            avg = sum(vals) / n
            if n < 2:
                return float(avg)
            variance = sum((v - avg) ** 2 for v in vals) / (n - 1)
            std = variance**0.5
            direction = 1.0 if avg >= overall else -1.0
            shrink = (n - 1) / n
            return float(avg + direction * self._STD_AMPLIFICATION * shrink * std)

        result = []
        for h in range(hours):
            step_time = start_time + timedelta(hours=h)
            hour = step_time.hour
            dow = step_time.weekday()
            price: float | None = None

            # 1. Weather-corrected lookup
            if (
                ghi_forecast is not None
                and wind_forecast is not None
                and h < len(ghi_forecast)
                and h < len(wind_forecast)
            ):
                gb = self._ghi_bin(ghi_forecast[h])
                wb = self._wind_bin(wind_forecast[h])
                vals = self._weather_pattern.get((hour, dow, gb, wb), [])
                if len(vals) >= self._MIN_SAMPLES:
                    price = _sharpen(vals)

            # 2. Simple (hour, dow) historical average
            if price is None:
                vals = self._simple_pattern.get((hour, dow), [])
                if len(vals) >= self._MIN_SAMPLES:
                    price = _sharpen(vals)

            # 3. Overall average (last resort)
            if price is None:
                price = overall

            result.append(round(price, 4))

        return result


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

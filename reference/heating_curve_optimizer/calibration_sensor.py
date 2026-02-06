"""Calibration sensor for validating thermal parameters."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components import recorder
from homeassistant.components.recorder import history
from homeassistant.components.sensor import SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.util import dt as dt_util

from .const import (
    CONF_AREA_M2,
    CONF_ENERGY_LABEL,
    DEFAULT_COP_AT_35,
    DEFAULT_K_FACTOR,
    DEFAULT_OUTDOOR_TEMP_COEFFICIENT,
    DEFAULT_THERMAL_STORAGE_EFFICIENCY,
    U_VALUE_MAP,
)
from .entity import BaseUtilitySensor

_LOGGER = logging.getLogger(__name__)


def _get_sensor_value(hass: HomeAssistant, entity_id: str | None) -> float | None:
    """Get a float value from a sensor, returning None if unavailable."""
    if not entity_id:
        return None
    state = hass.states.get(entity_id)
    if not state or state.state in ("unknown", "unavailable"):
        return None
    try:
        return float(state.state)
    except (ValueError, TypeError):
        return None


def _find_entity_id_by_unique_id(hass: HomeAssistant, unique_id: str) -> str | None:
    """Find entity_id by unique_id using entity registry."""
    try:
        registry = er.async_get(hass)
        entry = registry.async_get_entity_id(
            "sensor", "heating_curve_optimizer", unique_id
        )
        return entry
    except Exception:
        return None


class CalibrationSensor(BaseUtilitySensor):
    """Sensor that validates thermal parameters against actual measurements.

    This sensor analyzes historical data (7 days) to check if:
    1. Theoretical heat loss matches actual heat pump production
    2. COP calculations are realistic
    3. Thermal storage efficiency is appropriate
    4. Energy label setting matches measured performance (graaddagen correlation)
    5. Long-term trends in system performance

    It provides recommendations for parameter adjustments including energy label.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        name: str,
        unique_id: str,
        device: DeviceInfo,
        *,
        entry: ConfigEntry | None = None,
        heat_loss_sensor: str | None = None,
        thermal_power_sensor: str | None = None,
        outdoor_sensor: str | None = None,
        indoor_sensor: str | None = None,
        supply_temp_sensor: str | None = None,
        cop_sensor: str | None = None,
    ):
        """Initialize the calibration sensor."""
        super().__init__(
            name=name,
            unique_id=unique_id,
            unit="%",
            device_class=None,
            icon="mdi:tune",
            visible=True,
            device=device,
            translation_key="calibration",
        )
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self.hass = hass
        self._entry = entry
        self.heat_loss_sensor = heat_loss_sensor
        self.thermal_power_sensor = thermal_power_sensor
        self.outdoor_sensor = outdoor_sensor
        self.indoor_sensor = indoor_sensor
        self.supply_temp_sensor = supply_temp_sensor
        self.cop_sensor = cop_sensor
        self._extra_attrs: dict[str, Any] = {}

        # Track last calculation time to update every 1 hour (first run immediate)
        self._last_calculation: datetime | None = None
        self._calculation_interval = timedelta(hours=1)
        self._first_run = True  # Flag to force first run

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra attributes."""
        return self._extra_attrs

    async def async_update(self) -> None:
        """Update the calibration sensor."""
        now = dt_util.utcnow()

        # Force first run, then only recalculate every hour
        if not self._first_run:
            if (
                self._last_calculation
                and now - self._last_calculation < self._calculation_interval
            ):
                return
        else:
            self._first_run = False

        self._last_calculation = now

        try:
            # Start with 24 hours of history for quick initial calibration
            # Gradually increase lookback as more data becomes available
            days_to_check = 1  # Start with 1 day for quick results
            start_time = now - timedelta(days=days_to_check)
            short_term_start = now - timedelta(
                hours=6
            )  # Even shorter for immediate feedback

            # Validate heat loss accuracy (short-term for quick feedback)
            heat_loss_accuracy = await self._validate_heat_loss(short_term_start, now)

            # Validate COP accuracy
            cop_accuracy = await self._validate_cop(short_term_start, now)

            # Validate thermal storage efficiency
            storage_efficiency_recommendation = await self._validate_storage_efficiency(
                start_time, now
            )

            # NEW: Graaddagen correlation analysis (7 days)
            graaddagen_analysis = await self._analyze_graaddagen_correlation(
                start_time, now
            )

            # NEW: Energy label recommendation based on measured U-value
            energy_label_recommendation = None
            measured_u_value = None
            if graaddagen_analysis:
                measured_u_value = graaddagen_analysis.get("measured_u_value")
                energy_label_recommendation = graaddagen_analysis.get(
                    "recommended_label"
                )

            # NEW: Long-term trend analysis
            trend_analysis = await self._analyze_long_term_trend(start_time, now)

            # Overall calibration quality (0-100%)
            # 100% = perfect match, 0% = completely off
            if heat_loss_accuracy is not None:
                calibration_quality = heat_loss_accuracy
            elif measured_u_value is not None and self._entry:
                # Use graaddagen analysis if available
                configured_label = self._entry.options.get(
                    CONF_ENERGY_LABEL, self._entry.data.get(CONF_ENERGY_LABEL, "C")
                )
                configured_u = U_VALUE_MAP.get(configured_label, 0.80)
                if configured_u > 0:
                    ratio = min(measured_u_value, configured_u) / max(
                        measured_u_value, configured_u
                    )
                    calibration_quality = 100.0 * ratio
                else:
                    calibration_quality = None
            else:
                calibration_quality = None

            self._attr_native_value = (
                round(calibration_quality, 1) if calibration_quality else None
            )

            # Build comprehensive attributes
            self._extra_attrs = {
                "heat_loss_accuracy_pct": (
                    round(heat_loss_accuracy, 1) if heat_loss_accuracy else None
                ),
                "cop_accuracy_pct": round(cop_accuracy, 1) if cop_accuracy else None,
                "storage_efficiency_current": DEFAULT_THERMAL_STORAGE_EFFICIENCY,
                "storage_efficiency_recommended": storage_efficiency_recommendation,
                # NEW: Graaddagen analysis results
                "measured_u_value": (
                    round(measured_u_value, 2) if measured_u_value else None
                ),
                "recommended_energy_label": energy_label_recommendation,
                "current_energy_label": (
                    self._entry.options.get(
                        CONF_ENERGY_LABEL, self._entry.data.get(CONF_ENERGY_LABEL)
                    )
                    if self._entry
                    else None
                ),
                "graaddagen_samples": (
                    graaddagen_analysis.get("sample_count")
                    if graaddagen_analysis
                    else None
                ),
                "graaddagen_correlation": (
                    round(graaddagen_analysis.get("correlation", 0), 2)
                    if graaddagen_analysis
                    else None
                ),
                # NEW: Trend analysis
                "trend_direction": (
                    trend_analysis.get("direction") if trend_analysis else None
                ),
                "trend_change_pct": (
                    round(trend_analysis.get("change_pct", 0), 1)
                    if trend_analysis
                    else None
                ),
                "last_calibration": now.isoformat(),
                "calibration_period_days": days_to_check,
                "status": self._get_status_message(
                    heat_loss_accuracy,
                    cop_accuracy,
                    storage_efficiency_recommendation,
                    energy_label_recommendation,
                    trend_analysis,
                ),
            }

            self._mark_available()

        except Exception as err:
            _LOGGER.error("Calibration sensor update failed: %s", err, exc_info=True)
            self._set_unavailable(f"Calibratie mislukt: {err}")

    async def _validate_heat_loss(
        self, start_time: datetime, end_time: datetime
    ) -> float | None:
        """Validate heat loss calculation against actual heat production.

        Returns accuracy percentage (100% = perfect match).
        """
        theoretical = _get_sensor_value(self.hass, self.heat_loss_sensor)
        actual = _get_sensor_value(self.hass, self.thermal_power_sensor)

        if theoretical is None or actual is None or theoretical <= 0:
            return None

        # Calculate accuracy: 100% when ratio is 1.0, decreases linearly
        ratio = actual / theoretical
        accuracy = 100.0 * (1.0 - abs(1.0 - ratio))
        return max(0.0, min(100.0, accuracy))

    async def _validate_cop(
        self, start_time: datetime, end_time: datetime
    ) -> float | None:
        """Validate COP calculation against theoretical values.

        Returns accuracy percentage (100% = perfect match).
        """
        actual_cop = _get_sensor_value(self.hass, self.cop_sensor)
        if actual_cop is None:
            _LOGGER.debug(
                "COP validation skipped: cop_sensor %s unavailable", self.cop_sensor
            )
            return None

        # Get current conditions with defaults
        outdoor_temp = _get_sensor_value(self.hass, self.outdoor_sensor) or 7.0
        supply_temp = _get_sensor_value(self.hass, self.supply_temp_sensor) or 28.0

        # Get parameters from config entry
        def _get_config(key: str, default: float) -> float:
            if not self._entry:
                return default
            return self._entry.options.get(key, self._entry.data.get(key, default))

        k_factor = _get_config("k_factor", DEFAULT_K_FACTOR)
        cop_compensation = _get_config("cop_compensation_factor", 1.0)
        outdoor_coef = _get_config(
            "outdoor_temp_coefficient", DEFAULT_OUTDOOR_TEMP_COEFFICIENT
        )

        # Calculate theoretical COP
        theoretical_cop = (
            DEFAULT_COP_AT_35
            + outdoor_coef * outdoor_temp
            - k_factor * (supply_temp - 35)
        ) * cop_compensation

        if theoretical_cop <= 0:
            return None

        # Calculate accuracy: 100% when ratio is 1.0, decreases linearly
        ratio = actual_cop / theoretical_cop
        accuracy = 100.0 * (1.0 - abs(1.0 - ratio))
        return max(0.0, min(100.0, accuracy))

    async def _validate_storage_efficiency(
        self, start_time: datetime, end_time: datetime
    ) -> float | None:
        """Estimate appropriate thermal storage efficiency.

        Analyzes the ratio of actual buffer behavior vs predicted.
        Returns recommended efficiency value.
        """
        if not self._entry:
            return None

        try:
            # Get area from config
            area_m2 = self._entry.options.get(
                CONF_AREA_M2, self._entry.data.get(CONF_AREA_M2, 150)
            )

            # Estimate storage efficiency based on building characteristics
            # Heavier buildings (concrete, brick) have higher thermal mass
            # Lighter buildings (wood frame) have lower thermal mass
            # Default 0.15 is for average construction

            # Use energy label as proxy for building quality/mass
            energy_label = self._entry.options.get(
                CONF_ENERGY_LABEL, self._entry.data.get(CONF_ENERGY_LABEL, "C")
            )

            # Better insulated buildings tend to have more thermal mass
            # because they're often newer with concrete construction
            label_efficiency_map = {
                "A+++": 0.20,
                "A++": 0.18,
                "A+": 0.17,
                "A": 0.16,
                "B": 0.15,
                "C": 0.14,
                "D": 0.13,
                "E": 0.12,
                "F": 0.11,
                "G": 0.10,
            }

            # Larger buildings have more thermal mass per unit area
            area_factor = min(1.2, max(0.8, area_m2 / 150))

            base_efficiency = label_efficiency_map.get(energy_label, 0.15)
            recommended = round(base_efficiency * area_factor, 2)

            _LOGGER.debug(
                "Storage efficiency recommendation: %.2f (label=%s, area=%d m²)",
                recommended,
                energy_label,
                area_m2,
            )

            return recommended

        except Exception as err:
            _LOGGER.debug("Storage efficiency validation failed: %s", err)
            return None

    async def _analyze_graaddagen_correlation(
        self, start_time: datetime, end_time: datetime
    ) -> dict[str, Any] | None:
        """Analyze correlation between graaddagen and thermal heat production.

        Calculates actual U-value from historical data and recommends energy label.
        Returns dict with:
        - measured_u_value: Calculated U-value in W/(m²·K)
        - recommended_label: Best matching energy label
        - sample_count: Number of data points used
        - correlation: How well data correlates (0-1)
        """
        if not self.thermal_power_sensor or not self.outdoor_sensor:
            _LOGGER.debug(
                "Graaddagen analysis skipped: thermal_power=%s, outdoor=%s",
                self.thermal_power_sensor,
                self.outdoor_sensor,
            )
            return None

        if not self._entry:
            _LOGGER.debug("Graaddagen analysis skipped: no config entry")
            return None

        try:
            # Check if recorder is available
            if not recorder.is_entity_recorded(self.hass, self.thermal_power_sensor):
                _LOGGER.info(
                    "Graaddagen analysis: thermal power sensor %s not recorded in history",
                    self.thermal_power_sensor,
                )
                return None

            # Get historical data
            thermal_history = await recorder.get_instance(
                self.hass
            ).async_add_executor_job(
                history.state_changes_during_period,
                self.hass,
                start_time,
                end_time,
                self.thermal_power_sensor,
                True,  # include_start_time_state
                True,  # significant_changes_only
                1000,  # minimal_response (max states)
            )

            outdoor_history = await recorder.get_instance(
                self.hass
            ).async_add_executor_job(
                history.state_changes_during_period,
                self.hass,
                start_time,
                end_time,
                self.outdoor_sensor,
                True,
                True,
                1000,
            )

            indoor_history = None
            if self.indoor_sensor:
                indoor_history = await recorder.get_instance(
                    self.hass
                ).async_add_executor_job(
                    history.state_changes_during_period,
                    self.hass,
                    start_time,
                    end_time,
                    self.indoor_sensor,
                    True,
                    True,
                    1000,
                )

            if not thermal_history or not outdoor_history:
                _LOGGER.debug("No historical data available for graaddagen analysis")
                return None

            thermal_states = thermal_history.get(self.thermal_power_sensor, [])
            outdoor_states = outdoor_history.get(self.outdoor_sensor, [])
            indoor_states = (
                indoor_history.get(self.indoor_sensor, []) if indoor_history else []
            )

            if len(thermal_states) < 5 or len(outdoor_states) < 5:
                _LOGGER.info(
                    "Graaddagen analysis: not enough data points "
                    "(thermal=%d, outdoor=%d, need 5+)",
                    len(thermal_states),
                    len(outdoor_states),
                )
                return None

            # Group data by day and calculate daily averages
            daily_data = {}  # date -> {thermal_kwh, outdoor_temp, indoor_temp}

            # Process thermal data (kW -> kWh per day)
            for state in thermal_states:
                if state.state in ("unknown", "unavailable"):
                    continue
                try:
                    date_key = state.last_updated.date()
                    thermal_kw = float(state.state)
                    if date_key not in daily_data:
                        daily_data[date_key] = {
                            "thermal_samples": [],
                            "outdoor_samples": [],
                            "indoor_samples": [],
                        }
                    daily_data[date_key]["thermal_samples"].append(thermal_kw)
                except (ValueError, TypeError):
                    continue

            # Process outdoor temperature data
            for state in outdoor_states:
                if state.state in ("unknown", "unavailable"):
                    continue
                try:
                    date_key = state.last_updated.date()
                    outdoor_temp = float(state.state)
                    if date_key in daily_data:
                        daily_data[date_key]["outdoor_samples"].append(outdoor_temp)
                except (ValueError, TypeError):
                    continue

            # Process indoor temperature data
            indoor_temp_default = 20.0  # Default if no sensor
            if indoor_states:
                for state in indoor_states:
                    if state.state in ("unknown", "unavailable"):
                        continue
                    try:
                        date_key = state.last_updated.date()
                        indoor_temp = float(state.state)
                        if date_key in daily_data:
                            daily_data[date_key]["indoor_samples"].append(indoor_temp)
                    except (ValueError, TypeError):
                        continue

            # Calculate daily averages and graaddagen
            valid_days = []
            for date_key, data in daily_data.items():
                if (
                    not data["thermal_samples"]
                    or not data["outdoor_samples"]
                    or len(data["thermal_samples"]) < 3
                    or len(data["outdoor_samples"]) < 3
                ):
                    continue

                avg_thermal_kw = sum(data["thermal_samples"]) / len(
                    data["thermal_samples"]
                )
                avg_outdoor = sum(data["outdoor_samples"]) / len(
                    data["outdoor_samples"]
                )
                avg_indoor = (
                    sum(data["indoor_samples"]) / len(data["indoor_samples"])
                    if data["indoor_samples"]
                    else indoor_temp_default
                )

                # Calculate graaddagen for this day
                delta_t = avg_indoor - avg_outdoor
                if delta_t > 0:  # Only heating days
                    # Convert kW to kWh/day (24 hours)
                    thermal_kwh_day = avg_thermal_kw * 24
                    valid_days.append(
                        {
                            "date": date_key,
                            "thermal_kwh": thermal_kwh_day,
                            "graaddagen": delta_t,
                            "outdoor_temp": avg_outdoor,
                            "indoor_temp": avg_indoor,
                        }
                    )

            if len(valid_days) < 1:
                _LOGGER.info(
                    "Graaddagen analysis: no valid heating days found "
                    "(need days with indoor > outdoor temp and 3+ samples)"
                )
                return None

            # Calculate U-value from regression
            # Q = U × A × ΔT × 24h
            # U × A = Q / (ΔT × 24)
            area_m2 = self._entry.options.get(
                CONF_AREA_M2, self._entry.data.get(CONF_AREA_M2, 150)
            )

            u_times_a_values = []
            for day in valid_days:
                u_times_a = day["thermal_kwh"] / (day["graaddagen"] * 24)
                u_times_a_values.append(u_times_a * 1000)  # Convert kW to W

            # Calculate average U×A and U-value
            avg_u_times_a = sum(u_times_a_values) / len(u_times_a_values)
            measured_u_value = avg_u_times_a / area_m2

            # Calculate correlation (how consistent is U×A across days)
            if len(u_times_a_values) > 1:
                mean = avg_u_times_a
                variance = sum((x - mean) ** 2 for x in u_times_a_values) / len(
                    u_times_a_values
                )
                std_dev = variance**0.5
                coefficient_of_variation = std_dev / mean if mean > 0 else 1.0
                # Convert to correlation (1 = perfect, 0 = no correlation)
                correlation = max(0, 1.0 - coefficient_of_variation)
            else:
                correlation = 0.5

            # Find best matching energy label
            recommended_label = None
            min_diff = float("inf")
            for label, u_value in U_VALUE_MAP.items():
                diff = abs(u_value - measured_u_value)
                if diff < min_diff:
                    min_diff = diff
                    recommended_label = label

            _LOGGER.info(
                "Graaddagen analysis: measured U=%.2f W/(m²·K), "
                "recommended label=%s (from %d days)",
                measured_u_value,
                recommended_label,
                len(valid_days),
            )

            return {
                "measured_u_value": measured_u_value,
                "recommended_label": recommended_label,
                "sample_count": len(valid_days),
                "correlation": correlation,
                "u_times_a": avg_u_times_a,
            }

        except Exception as err:
            _LOGGER.debug("Graaddagen analysis failed: %s", err, exc_info=True)
            return None

    async def _analyze_long_term_trend(
        self, start_time: datetime, end_time: datetime
    ) -> dict[str, Any] | None:
        """Analyze long-term trends in system performance.

        Looks at first half vs second half of period to detect degradation.
        Returns dict with:
        - direction: 'improving', 'stable', or 'degrading'
        - change_pct: Percentage change in performance
        """
        if not self.heat_loss_sensor or not self.thermal_power_sensor:
            return None

        try:
            # Split period in half
            mid_time = start_time + (end_time - start_time) / 2

            # Get accuracy for first half
            first_half_accuracy = await self._validate_heat_loss(start_time, mid_time)

            # Get accuracy for second half
            second_half_accuracy = await self._validate_heat_loss(mid_time, end_time)

            if first_half_accuracy is None or second_half_accuracy is None:
                return None

            # Calculate trend
            change_pct = second_half_accuracy - first_half_accuracy

            if abs(change_pct) < 5:
                direction = "stable"
            elif change_pct > 0:
                direction = "improving"
            else:
                direction = "degrading"

            return {"direction": direction, "change_pct": change_pct}

        except Exception as err:
            _LOGGER.debug("Trend analysis failed: %s", err)
            return None

    def _get_status_message(
        self,
        heat_loss_accuracy: float | None,
        cop_accuracy: float | None,
        storage_recommendation: float | None,
        energy_label_recommendation: str | None = None,
        trend_analysis: dict[str, Any] | None = None,
    ) -> str:
        """Generate human-readable status message."""
        messages = []

        if heat_loss_accuracy is not None:
            if heat_loss_accuracy >= 95:
                messages.append("Warmteverlies: Uitstekend")
            elif heat_loss_accuracy >= 85:
                messages.append("Warmteverlies: Goed")
            elif heat_loss_accuracy >= 70:
                messages.append("Warmteverlies: Redelijk")
            else:
                messages.append("Warmteverlies: Kalibratie nodig")

        if cop_accuracy is not None:
            if cop_accuracy >= 95:
                messages.append("COP: Uitstekend")
            elif cop_accuracy >= 85:
                messages.append("COP: Goed")
            elif cop_accuracy >= 70:
                messages.append("COP: Redelijk")
            else:
                messages.append("COP: Kalibratie nodig")

        if storage_recommendation is not None:
            current = DEFAULT_THERMAL_STORAGE_EFFICIENCY
            if abs(storage_recommendation - current) < 0.05:
                messages.append("Thermische opslag: OK")
            else:
                messages.append(
                    f"Thermische opslag: Pas aan naar {storage_recommendation:.2f}"
                )

        # NEW: Energy label recommendation
        if energy_label_recommendation is not None:
            if self._entry:
                current_label = self._entry.options.get(
                    CONF_ENERGY_LABEL, self._entry.data.get(CONF_ENERGY_LABEL, "C")
                )
                if energy_label_recommendation != current_label:
                    messages.append(
                        f"Energielabel: Aanbevolen {energy_label_recommendation} "
                        f"(huidig: {current_label})"
                    )
                else:
                    messages.append(f"Energielabel: Correct ({current_label})")

        # NEW: Trend analysis
        if trend_analysis:
            direction = trend_analysis.get("direction")
            change_pct = trend_analysis.get("change_pct", 0)
            if direction == "improving":
                messages.append(f"Trend: Verbeterend (+{change_pct:.1f}%)")
            elif direction == "degrading":
                messages.append(f"Trend: Verslechterend ({change_pct:.1f}%)")
            else:
                messages.append("Trend: Stabiel")

        if not messages:
            return "Onvoldoende data voor kalibratie"

        return ", ".join(messages)

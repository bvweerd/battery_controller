"""Sensor platform for Battery Controller integration."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any, cast

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.const import PERCENTAGE, EntityCategory
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import (
    ACTION_CHARGING,
    ACTION_DISCHARGING,
    ACTION_IDLE,
    BATTERY_SUBENTRY_TYPE,
    DOMAIN,
    PV_SUBENTRY_TYPE,
)
from .coordinator import ForecastCoordinator, OptimizationCoordinator

_LOGGER = logging.getLogger(__name__)

# All entities are updated by the coordinator (push model); no parallel polling.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Battery Controller sensors from a config entry."""
    data = entry.runtime_data
    optimization_coordinator = data.optimization_coordinator
    forecast_coordinator = data.forecast_coordinator
    device = data.device
    battery_devices = data.battery_devices
    pv_devices = data.pv_devices

    # Main-device sensors (associated with the config entry, not any subentry)
    async_add_entities(
        [
            # Optimization output sensors
            BatteryOptimalPowerSensor(optimization_coordinator, device, entry),
            BatteryOptimalModeSensor(optimization_coordinator, device, entry),
            BatteryScheduleSensor(optimization_coordinator, device, entry),
            # Battery state sensors
            BatterySoCSensor(optimization_coordinator, device, entry),
            BatteryPowerSensor(optimization_coordinator, device, entry),
            # Forecast sensors (use ForecastCoordinator)
            PVForecastSensor(forecast_coordinator, device, entry),
            ConsumptionForecastSensor(forecast_coordinator, device, entry),
            NetGridForecastSensor(forecast_coordinator, device, entry),
            # Weather logging sensors (stored in recorder for price model training)
            SolarIrradianceSensor(forecast_coordinator, device, entry),
            WindSpeedSensor(forecast_coordinator, device, entry),
            # Financial sensors
            BatteryDailySavingsSensor(optimization_coordinator, device, entry),
            BatteryShadowPriceSensor(optimization_coordinator, device, entry),
            # Grid control sensors
            CurrentGridPowerSensor(optimization_coordinator, device, entry),
            BatteryGridSetpointSensor(optimization_coordinator, device, entry),
            BatteryControlModeSensor(optimization_coordinator, device, entry),
            # Diagnostics
            OptimizationStatusSensor(optimization_coordinator, device, entry),
            ChargeEfficiencyCalibrationSensor(optimization_coordinator, device, entry),
            DischargeEfficiencyCalibrationSensor(
                optimization_coordinator, device, entry
            ),
        ]
    )

    # Per-battery subentry sensors — each call associates entities with that subentry
    for subentry in entry.subentries.values():
        if subentry.subentry_type == BATTERY_SUBENTRY_TYPE:
            batt_device = battery_devices.get(subentry.subentry_id, device)
            async_add_entities(
                [
                    BatterySubentrySetpointSensor(
                        optimization_coordinator,
                        batt_device,
                        entry,
                        subentry.subentry_id,
                        subentry.title,
                    ),
                    BatterySubentrySoCSensor(
                        optimization_coordinator,
                        batt_device,
                        entry,
                        subentry.subentry_id,
                        subentry.title,
                    ),
                    # Per-battery calibration: the fleet sensors average the
                    # packs together, which describes neither when they differ.
                    BatterySubentryChargeCalibrationSensor(
                        optimization_coordinator,
                        batt_device,
                        entry,
                        subentry.subentry_id,
                        subentry.title,
                    ),
                    BatterySubentryDischargeCalibrationSensor(
                        optimization_coordinator,
                        batt_device,
                        entry,
                        subentry.subentry_id,
                        subentry.title,
                    ),
                ],
                config_subentry_id=subentry.subentry_id,
            )

    # Per-PV-array subentry sensors. The forecast series is disabled by default
    # (large list attributes); the calibration sensor is not — it is a single
    # number, and it is the only place a user can see whether the array's
    # forecast is being corrected and why.
    for subentry in entry.subentries.values():
        if subentry.subentry_type == PV_SUBENTRY_TYPE:
            pv_device = pv_devices.get(subentry.subentry_id, device)
            async_add_entities(
                [
                    PVArrayForecastSensor(
                        forecast_coordinator,
                        pv_device,
                        entry,
                        subentry.subentry_id,
                        subentry.title,
                    ),
                    PVArrayCalibrationSensor(
                        forecast_coordinator,
                        pv_device,
                        entry,
                        subentry.subentry_id,
                        subentry.title,
                    ),
                ],
                config_subentry_id=subentry.subentry_id,
            )

    # Migration: remove legacy None-subentry device associations.
    # Before per-subentry device support, battery/PV devices were registered under the
    # main config entry (subentry=None). After async_add_entities with config_subentry_id,
    # the device has both None and the subentry ID in its associations, causing it to
    # appear in both "not under a sub-item" and the correct subentry in the HA UI.
    device_registry = dr.async_get(hass)
    for sid in list(battery_devices) + list(pv_devices):
        dev = device_registry.async_get_device(identifiers={(DOMAIN, sid)})
        if dev and None in dev.config_entries_subentries.get(entry.entry_id, set()):
            device_registry.async_update_device(
                dev.id,
                remove_config_entry_id=entry.entry_id,
                remove_config_subentry_id=None,
            )


class BatteryControllerSensor(CoordinatorEntity[OptimizationCoordinator], SensorEntity):
    """Base class for Battery Controller sensors backed by OptimizationCoordinator.

    Subclasses set ``_key``, the entity's suffix in the unique ID. Per-subentry
    sensors, whose key depends on the subentry, pass it to ``__init__`` instead.
    """

    _attr_has_entity_name = True
    coordinator: OptimizationCoordinator
    _key: str = ""

    def __init__(
        self,
        coordinator: OptimizationCoordinator,
        device: DeviceInfo,
        entry: ConfigEntry,
        key: str | None = None,
    ):
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_device_info = device
        if key is not None:
            self._key = key
        self._attr_unique_id = f"{entry.entry_id}_{self._key}"

    def _get_optimization_result(self) -> Any:
        """Get the latest optimization result from the optimization coordinator."""
        if self.coordinator and self.coordinator.data:
            return self.coordinator.data.get("optimization_result")
        return None


class BatteryForecastSensor(CoordinatorEntity[ForecastCoordinator], SensorEntity):
    """Base class for Battery Controller sensors backed by ForecastCoordinator."""

    _attr_has_entity_name = True
    coordinator: ForecastCoordinator
    _key: str = ""

    def __init__(
        self,
        coordinator: ForecastCoordinator,
        device: DeviceInfo,
        entry: ConfigEntry,
        key: str | None = None,
    ):
        """Initialize the forecast sensor."""
        super().__init__(coordinator)
        self._attr_device_info = device
        if key is not None:
            self._key = key
        self._attr_unique_id = f"{entry.entry_id}_{self._key}"


class BatteryOptimalPowerSensor(BatteryControllerSensor):
    """Sensor for recommended battery power.

    Positive = discharge, Negative = charge (matches battery_setpoint convention).
    """

    _attr_translation_key = "optimal_power"
    _attr_name = "Optimal Power"
    _attr_native_unit_of_measurement = "W"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _key = "optimal_power"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        # Convert kW to W and invert sign for consistency with battery_setpoint
        # Optimizer uses (positive=charge, negative=discharge)
        # Sensor uses (positive=discharge, negative=charge)
        value = -float(self.coordinator.data.get("optimal_power_kw", 0.0)) * 1000
        return round(value, 0) or 0.0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if self.coordinator.data is None:
            return {}
        return {
            "optimal_mode": self.coordinator.data.get("optimal_mode", ACTION_IDLE),
            "current_price": self.coordinator.data.get("current_price", 0.0),
        }


class BatteryOptimalModeSensor(BatteryControllerSensor):
    """Sensor for recommended battery mode."""

    _attr_translation_key = "optimal_mode"
    _attr_name = "Optimal Mode"
    _key = "optimal_mode"

    @property
    def native_value(self) -> str | None:
        if self.coordinator.data is None:
            return None
        return str(self.coordinator.data.get("optimal_mode", ACTION_IDLE))


class BatteryScheduleSensor(BatteryControllerSensor):
    """Sensor for the full battery schedule (as attributes)."""

    _attr_translation_key = "schedule"
    _attr_name = "Schedule"
    # Large list attributes (96-step schedules); disable by default to reduce
    # recorder load. Users who need these can enable them explicitly.
    _attr_entity_registry_enabled_default = False
    _key = "schedule"

    @property
    def native_value(self) -> str | None:
        if self.coordinator.data is None:
            return None
        mode_schedule = self.coordinator.data.get("mode_schedule", [])
        n_charging = sum(1 for m in mode_schedule if m == ACTION_CHARGING)
        n_discharging = sum(1 for m in mode_schedule if m == ACTION_DISCHARGING)
        n_idle = sum(1 for m in mode_schedule if m == ACTION_IDLE)
        return f"C:{n_charging} D:{n_discharging} I:{n_idle}"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if self.coordinator.data is None:
            return {}
        result = self.coordinator.data.get("optimization_result")
        attrs = {
            "step_start_times_iso": self.coordinator.data.get(
                "step_start_times_iso", []
            ),
            "step_durations_hours": self.coordinator.data.get(
                "step_durations_hours", []
            ),
            "power_schedule_kw": [
                -v for v in self.coordinator.data.get("power_schedule_kw", [])
            ],
            "mode_schedule": self.coordinator.data.get("mode_schedule", []),
            "soc_schedule_kwh": self.coordinator.data.get("soc_schedule_kwh", []),
        }
        if result is not None:
            attrs["grid_price_forecast"] = result.price_forecast
            attrs["pv_forecast_kw"] = result.pv_forecast
            attrs["consumption_forecast_kw"] = result.consumption_forecast
        price_forecast_model = self.coordinator.data.get("price_forecast_model")
        if price_forecast_model is not None:
            attrs["grid_price_forecast_predicted"] = price_forecast_model
        feed_in_price_forecast = self.coordinator.data.get("feed_in_price_forecast")
        if feed_in_price_forecast is not None:
            attrs["feed_in_price_forecast"] = feed_in_price_forecast
        feed_in_price_forecast_model = self.coordinator.data.get(
            "feed_in_price_forecast_model"
        )
        if feed_in_price_forecast_model is not None:
            attrs["feed_in_price_forecast_predicted"] = feed_in_price_forecast_model
        return attrs


class BatterySoCSensor(BatteryControllerSensor):
    """Sensor for combined battery state of charge across all batteries."""

    _attr_translation_key = "soc"
    _attr_name = "Total State of Charge"
    _attr_native_unit_of_measurement = "%"
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _key = "soc"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        battery_state = self.coordinator.data.get("battery_state")
        if battery_state:
            return float(round(battery_state.soc_percent, 1))
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if self.coordinator.data is None:
            return {}
        battery_state = self.coordinator.data.get("battery_state")
        if battery_state:
            return {
                "soc_kwh": round(battery_state.soc_kwh, 3),
                "power_kw": round(battery_state.power_kw, 3),
                "mode": battery_state.mode,
            }
        return {}


class BatteryPowerSensor(BatteryControllerSensor):
    """Sensor for current battery power."""

    _attr_translation_key = "battery_power"
    _attr_name = "Total Battery Power"
    _attr_native_unit_of_measurement = "kW"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _key = "battery_power"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        battery_state = self.coordinator.data.get("battery_state")
        if battery_state:
            return float(round(battery_state.power_kw, 3))
        return None


class PVForecastSensor(BatteryForecastSensor):
    """Sensor for PV production forecast.

    Reports total panel production: AC-coupled plus DC-coupled arrays. The
    state used to be the AC series alone, which reads a permanent 0 kW on a
    fully DC-coupled system even while the panels are at full output — there
    the AC series is legitimately empty and everything sits in the DC series.
    The AC/DC split stays available in the attributes.

    No inverter derating is applied here: this is what the panels produce,
    not what reaches the AC bus. Grid exchange is the net load sensor's job,
    and that one does apply DC_TO_AC_INVERTER_EFFICIENCY.
    """

    _attr_translation_key = "pv_forecast"
    _attr_name = "PV Forecast"
    _attr_native_unit_of_measurement = "kW"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _key = "pv_forecast"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        ac_kw = self.coordinator.data.get("current_pv_kw", 0.0) or 0.0
        dc_kw = self.coordinator.data.get("current_dc_pv_kw", 0.0) or 0.0
        return round(float(ac_kw) + float(dc_kw), 3)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if self.coordinator.data is None:
            return {}
        ac_forecast = self.coordinator.data.get("pv_forecast_kw", [])
        attrs: dict[str, Any] = {
            "forecast_kw": ac_forecast,
            "forecast_interval_minutes": self.coordinator.data.get(
                "forecast_interval_minutes", 60
            ),
            "current_ac_pv_kw": round(
                float(self.coordinator.data.get("current_pv_kw", 0.0) or 0.0), 3
            ),
        }
        dc_forecast = self.coordinator.data.get("pv_dc_forecast_kw", [])
        if dc_forecast and any(v > 0 for v in dc_forecast):
            attrs["dc_forecast_kw"] = dc_forecast
            attrs["current_dc_pv_kw"] = self.coordinator.data.get(
                "current_dc_pv_kw", 0.0
            )
            # Total series, so a DC-coupled system can chart production
            # without having to add the two series itself.
            attrs["total_forecast_kw"] = [
                round(a + d, 3) for a, d in zip(ac_forecast, dc_forecast)
            ]
        return attrs


class ConsumptionForecastSensor(BatteryForecastSensor):
    """Sensor for consumption forecast."""

    _attr_translation_key = "consumption_forecast"
    _attr_name = "Consumption Forecast"
    _attr_native_unit_of_measurement = "kW"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _key = "consumption_forecast"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        return cast(
            float | None, self.coordinator.data.get("current_consumption_kw", 0.0)
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if self.coordinator.data is None:
            return {}
        return {
            "forecast_kw": self.coordinator.data.get("consumption_forecast_kw", []),
            "forecast_interval_minutes": self.coordinator.data.get(
                "forecast_interval_minutes", 60
            ),
        }


class NetGridForecastSensor(BatteryForecastSensor):
    """Sensor for net grid power forecast (without battery = consumption - PV)."""

    _attr_translation_key = "net_grid_forecast"
    _attr_name = "Net Grid Forecast"
    _attr_native_unit_of_measurement = "kW"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _key = "net_grid_forecast"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        return cast(float | None, self.coordinator.data.get("current_net_load_kw", 0.0))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if self.coordinator.data is None:
            return {}
        return {
            "forecast_kw": self.coordinator.data.get("net_load_forecast_kw", []),
            "forecast_interval_minutes": self.coordinator.data.get(
                "forecast_interval_minutes", 60
            ),
        }


class SolarIrradianceSensor(BatteryForecastSensor):
    """Sensor for solar irradiance (GHI) — logged to recorder for price model training."""

    _attr_translation_key = "ghi"
    _attr_name = "Solar Irradiance"
    _attr_native_unit_of_measurement = "W/m²"
    _attr_device_class = SensorDeviceClass.IRRADIANCE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _key = "ghi"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        return cast(float | None, self.coordinator.data.get("current_ghi_wm2"))


class WindSpeedSensor(BatteryForecastSensor):
    """Sensor for wind speed — logged to recorder for price model training."""

    _attr_translation_key = "wind_speed_ms"
    _attr_name = "Wind Speed"
    _attr_native_unit_of_measurement = "m/s"
    _attr_device_class = SensorDeviceClass.WIND_SPEED
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _key = "wind_speed_ms"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        return cast(float | None, self.coordinator.data.get("current_wind_speed_ms"))


class BatteryDailySavingsSensor(BatteryControllerSensor):
    """Estimated savings of the CURRENT plan over the optimization horizon.

    Not a running total. The value is a forward-looking estimate for the horizon
    the optimizer just solved (up to 36 h), recomputed from scratch on every run,
    so it moves both up and down as prices and forecasts change.

    That is why the state class is MEASUREMENT rather than TOTAL: TOTAL makes the
    recorder accumulate the difference between consecutive states into a
    long-term sum, which for a fluctuating forecast produces a meaningless
    running figure and an incorrect cost in the energy dashboard.
    """

    _attr_translation_key = "daily_savings"
    _attr_name = "Estimated Savings"
    _attr_native_unit_of_measurement = "EUR"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _key = "daily_savings"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        return round(float(self.coordinator.data.get("savings", 0.0)), 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if self.coordinator.data is None:
            return {}
        return {
            "baseline_cost": round(self.coordinator.data.get("baseline_cost", 0.0), 3),
            "optimized_cost": round(self.coordinator.data.get("total_cost", 0.0), 3),
        }


class BatteryShadowPriceSensor(BatteryControllerSensor):
    """Sensor for the shadow price (marginal value) of stored energy.

    Represents how much future electricity costs decrease per additional kWh
    stored in the battery right now, derived from the DP value function.

    Use as a decision threshold:
    - Charge when buy_price < shadow_price * sqrt(RTE): buying 1 kWh AC stores
      only sqrt(RTE) kWh, each worth the shadow price.
    - Export/discharge when feed_in_price > shadow_price / sqrt(RTE): taking
      1 kWh out of the battery yields only sqrt(RTE) kWh at the meter.
    """

    _attr_translation_key = "shadow_price"
    _attr_name = "Shadow Price of Storage"
    _attr_native_unit_of_measurement = "EUR/kWh"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _key = "shadow_price"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        return cast(float | None, self.coordinator.data.get("shadow_price_eur_kwh"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if self.coordinator.data is None:
            return {}
        shadow_price = self.coordinator.data.get("shadow_price_eur_kwh", 0.0)
        rte = self.coordinator.battery_config.round_trip_efficiency
        sqrt_rte_val = rte**0.5
        return {
            "shadow_price_eur_kwh": shadow_price,
            # Minimum sell price at which discharging/exporting captures full value:
            # 1 kWh drawn from the battery reaches the meter as sqrt(RTE) kWh, so
            # the sell price must exceed lambda / sqrt(RTE) to beat holding it.
            "discharge_threshold_eur_kwh": (
                round(shadow_price / sqrt_rte_val, 4) if sqrt_rte_val > 0 else None
            ),
            # Maximum buy price at which charging is still economically justified:
            # 1 kWh bought stores only sqrt(RTE) kWh, worth lambda * sqrt(RTE).
            "charge_threshold_eur_kwh": round(shadow_price * sqrt_rte_val, 4),
        }


class CurrentGridPowerSensor(BatteryControllerSensor):
    """Sensor for current grid power (import/export)."""

    _attr_translation_key = "current_grid_power"
    _attr_name = "Current Grid Power"
    _attr_native_unit_of_measurement = "kW"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _key = "current_grid_power"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        action = self.coordinator.data.get("control_action", {})
        return round(float(action.get("current_grid_w", 0.0)) / 1000, 3)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if self.coordinator.data is None:
            return {}
        action = self.coordinator.data.get("control_action", {})
        current_grid_kw = action.get("current_grid_w", 0.0) / 1000
        return {
            "direction": (
                "importing"
                if current_grid_kw > 0
                else "exporting"
                if current_grid_kw < 0
                else "balanced"
            ),
            "import_kw": round(max(0.0, current_grid_kw), 3),
            "export_kw": round(abs(min(0.0, current_grid_kw)), 3),
        }


class BatteryGridSetpointSensor(BatteryControllerSensor):
    """Sensor for the battery power setpoint (charge/discharge target).

    Positive = discharge, Negative = charge.

    Two modes:
    - With power sensors: real-time calculated setpoint (HA-controlled)
    - Without power sensors: 0 when optimal_mode is zero_grid (battery-controlled)
    """

    _attr_translation_key = "battery_setpoint"
    _attr_name = "Battery Setpoint"
    _attr_native_unit_of_measurement = "W"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _key = "battery_setpoint"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        action = self.coordinator.data.get("control_action", {})
        # Invert sign: controller uses (positive=charge, negative=discharge)
        # but sensor convention is (positive=discharge, negative=charge)
        # Use `or 0.0` to avoid -0.0 display
        value = -action.get("target_power_w", 0.0)
        return round(value, 0) or 0.0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if self.coordinator.data is None:
            return {}
        return cast(dict[str, Any], self.coordinator.data.get("control_action", {}))


class BatterySubentrySetpointSensor(BatteryControllerSensor):
    """Per-battery setpoint sensor based on headroom-split of the combined setpoint.

    Positive = discharge, Negative = charge (sensor convention).
    """

    _attr_native_unit_of_measurement = "W"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: OptimizationCoordinator,
        device: DeviceInfo,
        entry: ConfigEntry,
        subentry_id: str,
        battery_title: str,
    ):
        super().__init__(coordinator, device, entry, f"setpoint_{subentry_id}")
        self._subentry_id = subentry_id
        self._attr_name = f"Battery Setpoint {battery_title}"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        setpoints = self.coordinator.data.get("battery_setpoints", {})
        kw = setpoints.get(self._subentry_id, 0.0)
        # Invert sign: controller positive=charge → sensor positive=discharge
        return round(-kw * 1000, 0) or 0.0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if self.coordinator.data is None:
            return {}
        per_states = self.coordinator.data.get("per_battery_states", {})
        state = per_states.get(self._subentry_id)
        if state is None:
            return {}
        return {
            "soc_percent": round(state.soc_percent, 1),
            "soc_kwh": round(state.soc_kwh, 3),
            "power_kw": round(state.power_kw, 3),
            "mode": state.mode,
        }


class BatterySubentrySoCSensor(BatteryControllerSensor):
    """Per-battery state-of-charge sensor."""

    _attr_native_unit_of_measurement = "%"
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: OptimizationCoordinator,
        device: DeviceInfo,
        entry: ConfigEntry,
        subentry_id: str,
        battery_title: str,
    ):
        super().__init__(coordinator, device, entry, f"soc_{subentry_id}")
        self._subentry_id = subentry_id
        self._attr_name = f"State of Charge {battery_title}"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        per_states = self.coordinator.data.get("per_battery_states", {})
        state = per_states.get(self._subentry_id)
        if state is None:
            return None
        return float(round(state.soc_percent, 1))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if self.coordinator.data is None:
            return {}
        per_states = self.coordinator.data.get("per_battery_states", {})
        state = per_states.get(self._subentry_id)
        if state is None:
            return {}
        return {
            "soc_kwh": round(state.soc_kwh, 3),
            "power_kw": round(state.power_kw, 3),
            "mode": state.mode,
        }


class PVArrayForecastSensor(BatteryForecastSensor):
    """Diagnostic sensor for a single PV array's expected output and forecast."""

    _attr_native_unit_of_measurement = "kW"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(
        self,
        coordinator: ForecastCoordinator,
        device: DeviceInfo,
        entry: ConfigEntry,
        subentry_id: str,
        array_title: str,
    ):
        super().__init__(coordinator, device, entry, f"pv_array_forecast_{subentry_id}")
        self._subentry_id = subentry_id
        self._attr_name = f"PV Forecast {array_title}"

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        forecasts = self.coordinator.data.get("per_pv_array_forecasts", {})
        forecast = forecasts.get(self._subentry_id, [])
        return forecast[0] if forecast else 0.0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if self.coordinator.data is None:
            return {}
        forecasts = self.coordinator.data.get("per_pv_array_forecasts", {})
        return {
            "forecast_kw": forecasts.get(self._subentry_id, []),
            "forecast_interval_minutes": self.coordinator.data.get(
                "forecast_interval_minutes", 60
            ),
        }


class BatteryControlModeSensor(BatteryControllerSensor):
    """Sensor for the current control mode."""

    _attr_translation_key = "control_mode"
    _attr_name = "Control Mode"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _key = "control_mode"

    @property
    def native_value(self) -> str | None:
        if self.coordinator.data is None:
            return None
        return str(self.coordinator.data.get("control_mode", "hybrid"))


class OptimizationStatusSensor(BatteryControllerSensor):
    """Sensor for optimization status / diagnostics."""

    _attr_translation_key = "optimization_status"
    _attr_name = "Optimization Status"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False
    _key = "optimization_status"

    @property
    def native_value(self) -> str:
        """Return the native value of the sensor."""
        if self.coordinator.data is None:
            return "initializing"
        if not self.coordinator.optimization_enabled:
            return "disabled"
        if not self.coordinator.last_update_success:
            return "failed"
        last_success = self.coordinator.last_success_time
        if last_success is not None:
            interval = self.coordinator.update_interval or timedelta(minutes=15)
            age = dt_util.utcnow() - last_success
            if age > interval * 2.5:
                return "stale"
        return "ok"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the state attributes."""
        last_success = self.coordinator.last_success_time
        interval = self.coordinator.update_interval or timedelta(minutes=15)
        age_minutes = (
            round((dt_util.utcnow() - last_success).total_seconds() / 60, 1)
            if last_success is not None
            else None
        )
        attrs: dict[str, Any] = {
            "last_update_success": self.coordinator.last_update_success,
            "failure_reason": self.coordinator.last_failure_reason,
            "last_success": str(last_success) if last_success else None,
            "age_minutes": age_minutes,
            "update_interval_minutes": interval.total_seconds() / 60,
        }
        if self.coordinator.data is None:
            return attrs
        result = self.coordinator.data.get("optimization_result")
        if result is None:
            return attrs
        attrs.update(
            {
                "n_steps": len(result.power_schedule_kw),
                "total_cost": round(result.total_cost, 3),
                "baseline_cost": round(result.baseline_cost, 3),
                "savings": round(result.savings, 3),
                "current_price": self.coordinator.data.get("current_price", 0.0),
                "price_forecast_source": self.coordinator.data.get(
                    "price_forecast_source", "live"
                ),
                "timestamp": str(self.coordinator.data.get("timestamp", "")),
            }
        )
        return attrs


class BatteryEfficiencyCalibrationSensor(BatteryControllerSensor):
    """Base class for the learned battery efficiency corrections.

    The correction is reported as a percentage of nominal: 100% means the
    battery moves exactly as much energy within a step as the model assumes,
    96% means it manages 4% less and the DP plans accordingly.

    The number on its own is ambiguous — an untouched 100% looks identical to
    a measured 100% — so ``samples`` and ``last_result`` are published
    alongside it. Several values of ``last_result`` are permanent for a given
    setup rather than a passing condition: a DC-coupled system never samples
    at all, and a control mode that does not execute the DP schedule verbatim
    (zero_grid, manual) never will either.
    """

    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_suggested_display_precision = 1

    def _calibration(self) -> tuple[float, int, bool, str]:
        """Return (correction, samples, applied, last_result) for this direction."""
        raise NotImplementedError

    def _dispatch(self) -> tuple[float | None, int]:
        """Return (measured/commanded throughput, samples) for this direction."""
        raise NotImplementedError

    @property
    def native_value(self) -> float:
        correction, _samples, _applied, _result = self._calibration()
        return round(correction * 100, 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        correction, samples, applied, last_result = self._calibration()
        fidelity, fidelity_samples = self._dispatch()
        return {
            "correction_factor": round(correction, 4),
            "samples": samples,
            "applied": applied,
            "last_result": last_result,
            # Measured over commanded throughput from the energy counters.
            # Never applied to the curve: it sits on the same side of the
            # inverter as the setpoint, so it shows whether the device followed
            # its instruction, not what it lost carrying it out.
            "dispatch_fidelity": (round(fidelity, 4) if fidelity is not None else None),
            "dispatch_samples": fidelity_samples,
        }


class ChargeEfficiencyCalibrationSensor(BatteryEfficiencyCalibrationSensor):
    """Learned correction on the charge-side SoC transition, fleet-wide.

    The capacity-weighted aggregate of the per-battery corrections — what the
    DP is actually handed, since it plans one SoC for the whole fleet. The
    per-battery sensors say which pack the number came from.
    """

    _attr_translation_key = "charge_eff_correction"
    _attr_name = "Charge Efficiency Correction"
    _key = "charge_eff_correction"

    def _calibration(self) -> tuple[float, int, bool, str]:
        return (
            self.coordinator.charge_eff_correction,
            self.coordinator.charge_eff_sample_count,
            self.coordinator.charge_eff_applied,
            self.coordinator.charge_eff_last_result,
        )

    def _dispatch(self) -> tuple[float | None, int]:
        return self.coordinator.dispatch_fidelity(ACTION_CHARGING)


class DischargeEfficiencyCalibrationSensor(BatteryEfficiencyCalibrationSensor):
    """Learned correction on the discharge-side SoC transition, fleet-wide."""

    _attr_translation_key = "discharge_eff_correction"
    _attr_name = "Discharge Efficiency Correction"
    _key = "discharge_eff_correction"

    def _calibration(self) -> tuple[float, int, bool, str]:
        return (
            self.coordinator.discharge_eff_correction,
            self.coordinator.discharge_eff_sample_count,
            self.coordinator.discharge_eff_applied,
            self.coordinator.discharge_eff_last_result,
        )

    def _dispatch(self) -> tuple[float | None, int]:
        return self.coordinator.dispatch_fidelity(ACTION_DISCHARGING)


class BatterySubentryCalibrationSensor(BatteryEfficiencyCalibrationSensor):
    """One battery's own learned efficiency correction.

    Reported per battery because the causes are per battery: the dispatcher
    concentrates a setpoint on one pack at a time, so a single fleet number
    describes whichever machine happened to be picked. A pack that is losing
    capacity shows up here as a correction below 100% while its sibling stays
    at nominal — on the fleet sensor the two are averaged into something that
    describes neither.

    ``last_result`` says why a pack is not learning; ``battery_not_dispatched``
    is the common one and is not a fault — it means the other battery has been
    doing the work.
    """

    _action: str = ACTION_CHARGING

    def __init__(
        self,
        coordinator: OptimizationCoordinator,
        device: DeviceInfo,
        entry: ConfigEntry,
        subentry_id: str,
        battery_title: str,
    ):
        super().__init__(coordinator, device, entry, f"{self._key}_{subentry_id}")
        self._subentry_id = subentry_id
        self._attr_name = f"{self._title} {battery_title}"

    _title = ""

    def _calibration(self) -> tuple[float, int, bool, str]:
        return self.coordinator.battery_calibration_state(
            self._subentry_id, self._action
        )

    def _dispatch(self) -> tuple[float | None, int]:
        return self.coordinator.battery_dispatch_fidelity(
            self._subentry_id, self._action
        )


class BatterySubentryChargeCalibrationSensor(BatterySubentryCalibrationSensor):
    """One battery's learned charge-side correction."""

    _attr_translation_key = "battery_charge_eff_correction"
    _action = ACTION_CHARGING
    _key = "charge_eff_correction"
    _title = "Charge Efficiency Correction"


class BatterySubentryDischargeCalibrationSensor(BatterySubentryCalibrationSensor):
    """One battery's learned discharge-side correction."""

    _attr_translation_key = "battery_discharge_eff_correction"
    _action = ACTION_DISCHARGING
    _key = "discharge_eff_correction"
    _title = "Discharge Efficiency Correction"


class PVArrayCalibrationSensor(BatteryForecastSensor):
    """Learned correction on one PV array's forecast.

    100% means the array produces what the model predicts. Below that the
    forecast is optimistic (soiling, a dead string, a wrong tilt or
    orientation entry); above it the array outperforms the model. Shading is
    a function of sun position rather than a constant gain, so it is not
    something this can capture.

    Reported per array because the causes are per array. ``applied`` stays
    False until the sample window has filled, and ``last_result`` says why an
    array is not learning — most often that it has no measured production
    sensor configured.
    """

    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_suggested_display_precision = 1

    def __init__(
        self,
        coordinator: ForecastCoordinator,
        device: DeviceInfo,
        entry: ConfigEntry,
        subentry_id: str,
        array_title: str,
    ):
        super().__init__(coordinator, device, entry, f"pv_calibration_{subentry_id}")
        self._subentry_id = subentry_id
        self._attr_name = f"PV Forecast Correction {array_title}"

    @property
    def native_value(self) -> float:
        return round(self.coordinator.pv_correction(self._subentry_id) * 100, 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "correction_factor": round(
                self.coordinator.pv_correction(self._subentry_id), 4
            ),
            "samples": self.coordinator.pv_sample_count(self._subentry_id),
            "applied": self.coordinator.pv_correction_applied(self._subentry_id),
            "last_result": self.coordinator.pv_last_result(self._subentry_id),
        }

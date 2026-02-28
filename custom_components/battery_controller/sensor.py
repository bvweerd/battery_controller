"""Sensor platform for Battery Controller integration."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry

from homeassistant.core import HomeAssistant

from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import OptimizationCoordinator
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

# All entities are updated by the coordinator (push model); no parallel polling.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Battery Controller sensors from a config entry."""
    data = entry.runtime_data
    optimization_coordinator = data["optimization_coordinator"]
    forecast_coordinator = data["forecast_coordinator"]
    device = data["device"]

    sensors: list[SensorEntity] = [
        # Optimization output sensors
        BatteryOptimalPowerSensor(optimization_coordinator, device, entry),
        BatteryOptimalModeSensor(optimization_coordinator, device, entry),
        BatteryScheduleSensor(optimization_coordinator, device, entry),
        # Battery state sensors
        BatterySoCSensor(optimization_coordinator, device, entry),
        BatteryPowerSensor(optimization_coordinator, device, entry),
        # Forecast sensors
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
    ]

    async_add_entities(sensors)


class BatteryControllerSensor(CoordinatorEntity[OptimizationCoordinator], SensorEntity):
    """Base class for Battery Controller sensors."""

    _attr_has_entity_name = True
    coordinator: OptimizationCoordinator

    def __init__(
        self,
        coordinator: OptimizationCoordinator,
        device: DeviceInfo,
        entry: ConfigEntry,
        key: str,
    ):
        """Initialize the sensor."""
        super().__init__(coordinator)
        self._attr_device_info = device
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._entry_id = entry.entry_id
        self._key = key

    def _get_optimization_result(self):
        """Get the latest optimization result from the optimization coordinator."""
        if self.coordinator and self.coordinator.data:
            return self.coordinator.data.get("optimization_result")
        return None


class BatteryOptimalPowerSensor(BatteryControllerSensor):
    """Sensor for recommended battery power.

    Positive = discharge, Negative = charge (matches battery_setpoint convention).
    """

    _attr_translation_key = "optimal_power"
    _attr_name = "Optimal Power"
    _attr_native_unit_of_measurement = "W"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, device, entry):
        super().__init__(coordinator, device, entry, "optimal_power")

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        # Convert kW to W and invert sign for consistency with battery_setpoint
        # Optimizer uses (positive=charge, negative=discharge)
        # Sensor uses (positive=discharge, negative=charge)
        value = -self.coordinator.data.get("optimal_power_kw", 0.0) * 1000
        return round(value, 0) or 0.0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if self.coordinator.data is None:
            return {}
        return {
            "optimal_mode": self.coordinator.data.get("optimal_mode", "idle"),
            "current_price": self.coordinator.data.get("current_price", 0.0),
        }


class BatteryOptimalModeSensor(BatteryControllerSensor):
    """Sensor for recommended battery mode."""

    _attr_translation_key = "optimal_mode"
    _attr_name = "Optimal Mode"

    def __init__(self, coordinator, device, entry):
        super().__init__(coordinator, device, entry, "optimal_mode")

    @property
    def native_value(self) -> str | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("optimal_mode", "idle")


class BatteryScheduleSensor(BatteryControllerSensor):
    """Sensor for the full battery schedule (as attributes)."""

    _attr_translation_key = "schedule"
    _attr_name = "Schedule"

    def __init__(self, coordinator, device, entry):
        super().__init__(coordinator, device, entry, "schedule")

    @property
    def native_value(self) -> str | None:
        if self.coordinator.data is None:
            return None
        mode_schedule = self.coordinator.data.get("mode_schedule", [])
        n_charging = sum(1 for m in mode_schedule if m == "charging")
        n_discharging = sum(1 for m in mode_schedule if m == "discharging")
        n_idle = sum(1 for m in mode_schedule if m == "idle")
        return f"C:{n_charging} D:{n_discharging} I:{n_idle}"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if self.coordinator.data is None:
            return {}
        result = self.coordinator.data.get("optimization_result")
        attrs = {
            "power_schedule_kw": self.coordinator.data.get("power_schedule_kw", []),
            "mode_schedule": self.coordinator.data.get("mode_schedule", []),
            "soc_schedule_kwh": self.coordinator.data.get("soc_schedule_kwh", []),
        }
        if result is not None:
            attrs["price_forecast"] = result.price_forecast
            attrs["pv_forecast_kw"] = result.pv_forecast
            attrs["consumption_forecast_kw"] = result.consumption_forecast
        return attrs


class BatterySoCSensor(BatteryControllerSensor):
    """Sensor for battery state of charge."""

    _attr_translation_key = "soc"
    _attr_name = "State of Charge"
    _attr_native_unit_of_measurement = "%"
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, device, entry):
        super().__init__(coordinator, device, entry, "soc")

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        battery_state = self.coordinator.data.get("battery_state")
        if battery_state:
            return round(battery_state.soc_percent, 1)
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
    _attr_name = "Battery Power"
    _attr_native_unit_of_measurement = "kW"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, device, entry):
        super().__init__(coordinator, device, entry, "battery_power")

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        battery_state = self.coordinator.data.get("battery_state")
        if battery_state:
            return round(battery_state.power_kw, 3)
        return None


class PVForecastSensor(BatteryControllerSensor):
    """Sensor for PV production forecast."""

    _attr_translation_key = "pv_forecast"
    _attr_name = "PV Forecast"
    _attr_native_unit_of_measurement = "kW"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, device, entry):
        super().__init__(coordinator, device, entry, "pv_forecast")

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("current_pv_kw", 0.0)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if self.coordinator.data is None:
            return {}
        attrs: dict[str, Any] = {
            "forecast_kw": self.coordinator.data.get("pv_forecast_kw", []),
        }
        dc_forecast = self.coordinator.data.get("pv_dc_forecast_kw", [])
        if dc_forecast and any(v > 0 for v in dc_forecast):
            attrs["dc_forecast_kw"] = dc_forecast
            attrs["current_dc_pv_kw"] = self.coordinator.data.get(
                "current_dc_pv_kw", 0.0
            )
        return attrs


class ConsumptionForecastSensor(BatteryControllerSensor):
    """Sensor for consumption forecast."""

    _attr_translation_key = "consumption_forecast"
    _attr_name = "Consumption Forecast"
    _attr_native_unit_of_measurement = "kW"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, device, entry):
        super().__init__(coordinator, device, entry, "consumption_forecast")

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("current_consumption_kw", 0.0)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if self.coordinator.data is None:
            return {}
        return {"forecast_kw": self.coordinator.data.get("consumption_forecast_kw", [])}


class NetGridForecastSensor(BatteryControllerSensor):
    """Sensor for net grid power forecast (without battery = consumption - PV)."""

    _attr_translation_key = "net_grid_forecast"
    _attr_name = "Net Grid Forecast"
    _attr_native_unit_of_measurement = "kW"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, device, entry):
        super().__init__(coordinator, device, entry, "net_grid_forecast")

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("current_net_load_kw", 0.0)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if self.coordinator.data is None:
            return {}
        return {"forecast_kw": self.coordinator.data.get("net_load_forecast_kw", [])}


class BatteryDailySavingsSensor(BatteryControllerSensor):
    """Sensor for daily savings from battery optimization."""

    _attr_translation_key = "daily_savings"
    _attr_name = "Estimated Savings"
    _attr_native_unit_of_measurement = "EUR"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(self, coordinator, device, entry):
        super().__init__(coordinator, device, entry, "daily_savings")

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        return round(self.coordinator.data.get("savings", 0.0), 2)

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
    - Charge when buy_price < shadow_price / sqrt(RTE)
    - Export/discharge when feed_in_price > shadow_price * sqrt(RTE)
    """

    _attr_translation_key = "shadow_price"
    _attr_name = "Shadow Price of Storage"
    _attr_native_unit_of_measurement = "EUR/kWh"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, device, entry):
        super().__init__(coordinator, device, entry, "shadow_price")

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("shadow_price_eur_kwh")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if self.coordinator.data is None:
            return {}
        shadow_price = self.coordinator.data.get("shadow_price_eur_kwh", 0.0)
        # Compute discharge and charge thresholds from battery config
        rte = (
            self.coordinator.battery_config.round_trip_efficiency
            if hasattr(self.coordinator, "battery_config")
            else 0.9
        )
        sqrt_rte_val = rte**0.5
        return {
            "shadow_price_eur_kwh": shadow_price,
            # Minimum sell price at which discharging/exporting captures full value
            "discharge_threshold_eur_kwh": round(shadow_price * sqrt_rte_val, 4),
            # Maximum buy price at which charging is still economically justified
            "charge_threshold_eur_kwh": (
                round(shadow_price / sqrt_rte_val, 4) if sqrt_rte_val > 0 else None
            ),
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

    def __init__(self, coordinator, device, entry):
        super().__init__(coordinator, device, entry, "current_grid_power")

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        action = self.coordinator.data.get("control_action", {})
        return round(action.get("current_grid_w", 0.0) / 1000, 3)

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

    def __init__(self, coordinator, device, entry):
        super().__init__(coordinator, device, entry, "battery_setpoint")

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        action = self.coordinator.data.get("control_action", {})
        # Invert sign: controller uses (positive=charge, negative=discharge)
        # but sensor convention is (positive=discharge, negative=charge)
        # Use abs(0.0) → 0.0 to avoid -0.0 display
        value = -action.get("target_power_w", 0.0)
        return round(value, 0) or 0.0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if self.coordinator.data is None:
            return {}
        return self.coordinator.data.get("control_action", {})


class BatteryControlModeSensor(BatteryControllerSensor):
    """Sensor for the current control mode."""

    _attr_translation_key = "control_mode"
    _attr_name = "Control Mode"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator, device, entry):
        super().__init__(coordinator, device, entry, "control_mode")

    @property
    def native_value(self) -> str | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("control_mode", "hybrid")


class OptimizationStatusSensor(BatteryControllerSensor):
    """Sensor for optimization status / diagnostics."""

    _attr_translation_key = "optimization_status"
    _attr_name = "Optimization Status"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator, device, entry):
        super().__init__(coordinator, device, entry, "optimization_status")

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


class SolarIrradianceSensor(BatteryControllerSensor):
    """Sensor for solar irradiance (GHI) — logged to recorder for price model training."""

    _attr_translation_key = "ghi"
    _attr_name = "Solar Irradiance"
    _attr_native_unit_of_measurement = "W/m²"
    _attr_device_class = SensorDeviceClass.IRRADIANCE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, device, entry):
        super().__init__(coordinator, device, entry, "ghi")

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("current_ghi_wm2")


class WindSpeedSensor(BatteryControllerSensor):
    """Sensor for wind speed — logged to recorder for price model training."""

    _attr_translation_key = "wind_speed_ms"
    _attr_name = "Wind Speed"
    _attr_native_unit_of_measurement = "m/s"
    _attr_device_class = SensorDeviceClass.WIND_SPEED
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, device, entry):
        super().__init__(coordinator, device, entry, "wind_speed_ms")

    @property
    def native_value(self) -> float | None:
        if self.coordinator.data is None:
            return None
        return self.coordinator.data.get("current_wind_speed_ms")

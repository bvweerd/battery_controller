"""Diagnostics sensor with all coordinator data."""

from __future__ import annotations

from typing import Any

from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from ..entity import BaseUtilitySensor


class CoordinatorDiagnosticsSensor(CoordinatorEntity, BaseUtilitySensor):
    """Diagnostics sensor with all coordinator data."""

    def __init__(
        self,
        weather_coordinator,
        heat_coordinator,
        optimization_coordinator,
        name: str,
        unique_id: str,
        device: DeviceInfo,
    ):
        """Initialize the diagnostics sensor."""
        # Use weather coordinator as primary
        CoordinatorEntity.__init__(self, weather_coordinator)
        BaseUtilitySensor.__init__(
            self,
            name=name,
            unique_id=unique_id,
            unit=None,  # No unit for string sensor
            device_class=None,
            icon="mdi:information-outline",
            visible=True,
            device=device,
            translation_key=name.lower().replace(" ", "_"),
        )
        self._attr_should_poll = False
        # Diagnostics sensor returns string, so no state_class or unit
        self._attr_state_class = None
        self._attr_native_unit_of_measurement = None
        self.weather_coordinator = weather_coordinator
        self.heat_coordinator = heat_coordinator
        self.optimization_coordinator = optimization_coordinator

    @property
    def native_value(self):
        """Return status based on coordinator states."""
        # Count successful coordinators
        success_count = sum(
            [
                self.weather_coordinator.last_update_success
                and self.weather_coordinator.data is not None,
                self.heat_coordinator.last_update_success
                and self.heat_coordinator.data is not None,
                self.optimization_coordinator.last_update_success
                and self.optimization_coordinator.data is not None,
            ]
        )

        if success_count == 3:
            return "OK"
        elif success_count == 2:
            return "PARTIAL"  # Optimization may still be loading
        elif success_count == 1:
            return "INITIALIZING"
        else:
            return "ERROR"

    @property
    def available(self) -> bool:
        """Always available to show diagnostics."""
        return True

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return all coordinator data as diagnostics."""
        attrs = {}

        # Coordinator status overview
        attrs["weather_available"] = (
            self.weather_coordinator.last_update_success
            and self.weather_coordinator.data is not None
        )
        attrs["heat_available"] = (
            self.heat_coordinator.last_update_success
            and self.heat_coordinator.data is not None
        )
        attrs["optimization_available"] = (
            self.optimization_coordinator.last_update_success
            and self.optimization_coordinator.data is not None
        )

        # Weather coordinator data
        if self.weather_coordinator.data:
            attrs["weather_last_update"] = str(
                self.weather_coordinator.data.get("timestamp")
            )
            attrs["outdoor_temperature"] = self.weather_coordinator.data.get(
                "current_temperature"
            )
        else:
            attrs["weather_status"] = "No data yet"

        # Heat coordinator data
        if self.heat_coordinator.data:
            attrs["heat_last_update"] = str(self.heat_coordinator.data.get("timestamp"))
            attrs["heat_loss"] = self.heat_coordinator.data.get("heat_loss")
            attrs["solar_gain"] = self.heat_coordinator.data.get("solar_gain")
            attrs["net_heat_loss"] = self.heat_coordinator.data.get("net_heat_loss")
        else:
            attrs["heat_status"] = "No data yet"

        # Optimization coordinator data
        if self.optimization_coordinator.data:
            attrs["optimization_last_update"] = str(
                self.optimization_coordinator.data.get("timestamp")
            )
            attrs["optimized_offset"] = self.optimization_coordinator.data.get(
                "optimized_offset"
            )
            attrs["total_cost"] = self.optimization_coordinator.data.get("total_cost")
            attrs["baseline_cost"] = self.optimization_coordinator.data.get(
                "baseline_cost"
            )
            attrs["cost_savings"] = self.optimization_coordinator.data.get(
                "cost_savings"
            )
        else:
            attrs["optimization_status"] = (
                "Waiting for first optimization run (starts within 5-10 seconds after startup)"
            )

        return attrs

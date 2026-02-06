"""Optimized supply temperature sensor using optimization coordinator."""

from __future__ import annotations

from typing import Any

from homeassistant.helpers.entity import DeviceInfo

from .base import BaseOptimizationSensor


class CoordinatorOptimizedSupplyTemperatureSensor(BaseOptimizationSensor):
    """Optimized supply temperature sensor using optimization coordinator."""

    _unrecorded_attributes = frozenset(
        {"optimized_offsets", "future_supply_temperatures"}
    )

    def __init__(
        self, coordinator, name: str, unique_id: str, icon: str, device: DeviceInfo
    ):
        """Initialize the sensor."""
        super().__init__(
            coordinator,
            name,
            unique_id,
            icon,
            device,
            unit="°C",
            device_class="temperature",
        )

    @property
    def native_value(self):
        """Return optimized supply temperature."""
        if not self.coordinator.data:
            return None

        future_temps = self.coordinator.data.get("future_supply_temperatures", [])
        if not future_temps:
            return None

        # Return first future supply temperature (current optimized temperature)
        return future_temps[0]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return forecast attributes."""
        if not self.coordinator.data:
            return {}
        return {
            "optimized_offsets": self.coordinator.data.get("optimized_offsets", []),
            "future_supply_temperatures": self.coordinator.data.get(
                "future_supply_temperatures", []
            ),
            "forecast_time_base": 60,
        }

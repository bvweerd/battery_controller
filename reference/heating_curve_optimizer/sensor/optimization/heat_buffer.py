"""Heat buffer sensor using optimization coordinator."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorStateClass
from homeassistant.helpers.entity import DeviceInfo

from .base import BaseOptimizationSensor


class CoordinatorHeatBufferSensor(BaseOptimizationSensor):
    """Heat buffer sensor using optimization coordinator."""

    _unrecorded_attributes = frozenset({"forecast"})

    def __init__(
        self, coordinator, name: str, unique_id: str, icon: str, device: DeviceInfo
    ):
        """Initialize the sensor."""
        # For energy device_class, state_class must be 'total' not 'measurement'
        super().__init__(
            coordinator,
            name,
            unique_id,
            icon,
            device,
            unit="kWh",
            device_class="energy",
            state_class=SensorStateClass.TOTAL,
        )

    @property
    def native_value(self):
        """Return current buffer level."""
        if not self.coordinator.data:
            return None
        buffer_evolution = self.coordinator.data.get("buffer_evolution", [])
        return buffer_evolution[0] if buffer_evolution else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return buffer evolution forecast."""
        if not self.coordinator.data:
            return {}
        return {
            "forecast": self.coordinator.data.get("buffer_evolution", []),
            "forecast_time_base": 60,
            "initial_buffer": self.coordinator.data.get("initial_buffer", 0.0),
        }

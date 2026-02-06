"""Cost savings sensor using optimization coordinator."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.helpers.entity import DeviceInfo

from .base import BaseOptimizationSensor


class CoordinatorCostSavingsSensor(BaseOptimizationSensor):
    """Cost savings forecast sensor showing predicted optimization savings in EUR."""

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
            unit="€",
            device_class=SensorDeviceClass.MONETARY,
            state_class=SensorStateClass.TOTAL,
        )

    @property
    def native_value(self):
        """Return cost savings in EUR."""
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("cost_savings", 0.0)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return cost breakdown."""
        if not self.coordinator.data:
            return {}

        data = self.coordinator.data
        baseline_cost = data.get("baseline_cost", 0.0)
        cost_savings = data.get("cost_savings", 0.0)
        savings_pct = (
            round(100 * cost_savings / baseline_cost, 1) if baseline_cost > 0 else 0.0
        )

        return {
            "total_cost_eur": round(data.get("total_cost", 0.0), 2),
            "baseline_cost_eur": round(baseline_cost, 2),
            "cost_savings_eur": round(cost_savings, 2),
            "savings_percentage": savings_pct,
            "planning_window_hours": len(data.get("optimized_offsets", [])),
        }

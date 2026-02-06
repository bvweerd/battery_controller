"""Heating curve offset sensor using optimization coordinator."""

from __future__ import annotations

from typing import Any

from homeassistant.helpers.entity import DeviceInfo

from .base import BaseOptimizationSensor


class CoordinatorHeatingCurveOffsetSensor(BaseOptimizationSensor):
    """Heating curve offset sensor using optimization coordinator."""

    _unrecorded_attributes = frozenset(
        {
            "optimized_offsets",
            "buffer_evolution",
            "future_supply_temperatures",
            "baseline_supply_temperatures",
            "prices",
            "demand_forecast",
            "baseline_cop",
            "optimized_cop",
            "outdoor_forecast",
        }
    )

    def __init__(
        self, coordinator, name: str, unique_id: str, icon: str, device: DeviceInfo
    ):
        """Initialize the sensor."""
        super().__init__(
            coordinator, name, unique_id, icon, device, unit="°C", device_class=None
        )

    @property
    def native_value(self):
        """Return optimized offset."""
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("optimized_offset")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return optimization results."""
        if not self.coordinator.data:
            return {}

        data = self.coordinator.data
        return {
            "optimized_offsets": data.get("optimized_offsets", []),
            "buffer_evolution": data.get("buffer_evolution", []),
            "initial_buffer": data.get("initial_buffer", 0.0),
            "previous_offset": data.get("previous_offset", 0),
            "future_supply_temperatures": data.get("future_supply_temperatures", []),
            "total_cost": data.get("total_cost", 0.0),
            "baseline_cost": data.get("baseline_cost", 0.0),
            "cost_savings": data.get("cost_savings", 0.0),
            "forecast_time_base": 60,
            "prices": data.get("prices", []),
            "demand_forecast": data.get("demand_forecast", []),
            "baseline_cop": data.get("baseline_cop", []),
            "optimized_cop": data.get("optimized_cop", []),
            "baseline_supply_temperatures": data.get(
                "baseline_supply_temperatures", []
            ),
            "outdoor_forecast": data.get("outdoor_forecast", []),
        }

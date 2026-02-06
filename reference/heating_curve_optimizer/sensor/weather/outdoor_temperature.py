"""Outdoor temperature sensor using weather coordinator."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorStateClass
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from ...entity import BaseUtilitySensor


class CoordinatorOutdoorTemperatureSensor(CoordinatorEntity, BaseUtilitySensor):
    """Outdoor temperature sensor using weather coordinator."""

    def __init__(self, coordinator, name: str, unique_id: str, device: DeviceInfo):
        """Initialize the sensor."""
        CoordinatorEntity.__init__(self, coordinator)
        BaseUtilitySensor.__init__(
            self,
            name=name,
            unique_id=unique_id,
            unit="°C",
            device_class="temperature",
            icon="mdi:thermometer",
            visible=True,
            device=device,
            translation_key=name.lower().replace(" ", "_").replace(".", "_"),
        )
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_should_poll = False

    @property
    def native_value(self):
        """Return current temperature."""
        if not self.coordinator.data:
            return None
        return self.coordinator.data.get("current_temperature")

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return (
            self.coordinator.last_update_success and self.coordinator.data is not None
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return forecast attributes."""
        if not self.coordinator.data:
            return {}
        return {
            "forecast": self.coordinator.data.get("temperature_forecast", []),
            "humidity_forecast": self.coordinator.data.get("humidity_forecast", []),
            "forecast_time_base": 60,
        }

"""Calculated supply temperature based on heating curve and outdoor temp."""

from __future__ import annotations

from homeassistant.components.sensor import SensorStateClass
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from ...entity import BaseUtilitySensor


class CoordinatorCalculatedSupplyTemperatureSensor(
    CoordinatorEntity, BaseUtilitySensor
):
    """Calculated supply temperature based on heating curve and outdoor temp."""

    def __init__(
        self,
        coordinator,
        name: str,
        unique_id: str,
        device: DeviceInfo,
        min_temp: float = 20.0,
        max_temp: float = 45.0,
        min_outdoor: float = -20.0,
        max_outdoor: float = 15.0,
    ):
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
            translation_key=name.lower().replace(" ", "_"),
        )
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_should_poll = False
        self.min_temp = min_temp
        self.max_temp = max_temp
        self.min_outdoor = min_outdoor
        self.max_outdoor = max_outdoor

    @property
    def native_value(self):
        """Calculate supply temperature from heating curve."""
        if not self.coordinator.data:
            return None

        # Get outdoor temperature
        outdoor_temp = self.coordinator.data.get("current_temperature")
        if outdoor_temp is None:
            return None

        # Calculate supply temperature using heating curve
        # Linear interpolation between min/max temps based on outdoor temp
        if outdoor_temp <= self.min_outdoor:
            supply_temp = self.max_temp
        elif outdoor_temp >= self.max_outdoor:
            supply_temp = self.min_temp
        else:
            # Linear interpolation
            temp_range = self.max_temp - self.min_temp
            outdoor_range = self.max_outdoor - self.min_outdoor
            supply_temp = self.max_temp - (
                (outdoor_temp - self.min_outdoor) / outdoor_range * temp_range
            )

        return round(supply_temp, 1)

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return (
            self.coordinator.last_update_success and self.coordinator.data is not None
        )

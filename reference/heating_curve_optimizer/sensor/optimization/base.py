"""Base class for optimization sensors using coordinator."""

from __future__ import annotations

from homeassistant.components.sensor import SensorStateClass
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from ...entity import BaseUtilitySensor


class BaseOptimizationSensor(CoordinatorEntity, BaseUtilitySensor):
    """Base class for optimization sensors using coordinator.

    Provides common initialization and availability logic for sensors
    that read from the OptimizationCoordinator.
    """

    def __init__(
        self,
        coordinator,
        name: str,
        unique_id: str,
        icon: str,
        device: DeviceInfo,
        *,
        unit: str = "°C",
        device_class: str | None = None,
        state_class: SensorStateClass = SensorStateClass.MEASUREMENT,
    ):
        """Initialize the sensor."""
        CoordinatorEntity.__init__(self, coordinator)
        BaseUtilitySensor.__init__(
            self,
            name=name,
            unique_id=unique_id,
            unit=unit,
            device_class=device_class,
            icon=icon,
            visible=True,
            device=device,
            translation_key=name.lower().replace(" ", "_"),
        )
        self._attr_state_class = state_class
        self._attr_should_poll = False

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return (
            self.coordinator.last_update_success and self.coordinator.data is not None
        )

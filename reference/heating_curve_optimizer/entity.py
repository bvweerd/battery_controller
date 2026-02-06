from __future__ import annotations


from homeassistant.components.sensor import (
    SensorEntity,
    RestoreEntity,
    SensorStateClass,
    SensorDeviceClass,
)
from homeassistant.helpers.entity import DeviceInfo


import logging

_LOGGER = logging.getLogger(__name__)


class BaseUtilitySensor(SensorEntity, RestoreEntity):
    def __init__(
        self,
        name: str | None,
        unique_id: str,
        unit: str,
        device_class: SensorDeviceClass | str | None,
        icon: str,
        visible: bool,
        device: DeviceInfo | None = None,
        translation_key: str | None = None,
    ):
        if name is not None:
            self._attr_name = name
        self._attr_translation_key = translation_key
        self._attr_has_entity_name = translation_key is not None
        self._attr_unique_id = unique_id
        self._attr_native_unit_of_measurement = unit
        if device_class is not None and not isinstance(device_class, SensorDeviceClass):
            device_class = SensorDeviceClass(device_class)
        self._attr_device_class = device_class
        self._attr_state_class = SensorStateClass.TOTAL
        self._attr_native_value = 0.0
        self._attr_available = True
        self._attr_icon = icon
        self._attr_entity_registry_enabled_default = visible
        self._attr_device_info = device
        self._last_unavailable_reason: str | None = None

    @property
    def native_value(self) -> float:
        return float(round(float(self._attr_native_value or 0.0), 8))

    async def async_added_to_hass(self):
        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state not in (
            "unknown",
            "unavailable",
        ):
            try:
                self._attr_native_value = float(last_state.state)
            except ValueError:
                self._attr_native_value = 0.0

    def reset(self):
        self._attr_native_value = 0.0
        self.async_write_ha_state()

    def set_value(self, value: float):
        self._attr_native_value = round(value, 8)
        self.async_write_ha_state()

    def _friendly_name(self) -> str:
        return (
            getattr(self, "_attr_name", None)
            or self.entity_id
            or self.__class__.__name__
        )

    def _set_unavailable(self, reason: str, *, level: int = logging.WARNING) -> None:
        """Mark the entity as unavailable and log the reason once."""

        self._attr_available = False
        if self._last_unavailable_reason == reason:
            return
        self._last_unavailable_reason = reason
        _LOGGER.log(level, "%s is niet beschikbaar: %s", self._friendly_name(), reason)

    def _mark_available(self) -> None:
        """Reset availability state."""

        self._last_unavailable_reason = None
        self._attr_available = True

    async def async_reset(self) -> None:
        """Async wrapper for reset."""
        self.reset()

    async def async_set_value(self, value: float) -> None:
        """Async wrapper for set_value."""
        self.set_value(value)


__all__ = ["BaseUtilitySensor"]

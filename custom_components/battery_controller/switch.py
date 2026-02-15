"""Switch platform for Battery Controller integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Battery Controller switch entities from a config entry."""
    data = entry.runtime_data
    device = data["device"]
    optimization_coordinator = data["optimization_coordinator"]

    entities = [
        BatteryOptimizationSwitch(hass, entry, device, optimization_coordinator),
    ]

    async_add_entities(entities)


class BatteryOptimizationSwitch(SwitchEntity):
    """Switch to enable/disable battery optimization."""

    _attr_has_entity_name = True
    _attr_translation_key = "optimization_enabled"
    _attr_name = "Optimization Enabled"
    _attr_icon = "mdi:battery-sync-outline"

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        device: DeviceInfo,
        optimization_coordinator,
    ):
        """Initialize the switch entity."""
        self.hass = hass
        self._entry = entry
        self._attr_device_info = device
        self._attr_unique_id = f"{entry.entry_id}_optimization_enabled"
        self._optimization_coordinator = optimization_coordinator
        self._is_on = True

    @property
    def is_on(self) -> bool:
        """Return true if optimization is enabled."""
        return self._is_on

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable optimization."""
        _LOGGER.info("Enabling battery optimization")
        self._is_on = True

        # Resume optimization updates
        self._optimization_coordinator.update_interval = (
            self._optimization_coordinator._original_interval
            if hasattr(self._optimization_coordinator, "_original_interval")
            else self._optimization_coordinator.update_interval
        )

        await self._optimization_coordinator.async_request_refresh()
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable optimization."""
        _LOGGER.info("Disabling battery optimization")
        self._is_on = False

        # Store original interval and stop updates
        if not hasattr(self._optimization_coordinator, "_original_interval"):
            self._optimization_coordinator._original_interval = (
                self._optimization_coordinator.update_interval
            )
        self._optimization_coordinator.update_interval = None

        self.async_write_ha_state()

"""Switch platform for Battery Controller integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0


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
    _attr_entity_category = EntityCategory.CONFIG

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
        """Enable optimization and immediately run a fresh cycle."""
        _LOGGER.info("Enabling battery optimization")
        self._is_on = True
        self._optimization_coordinator.optimization_enabled = True
        await self._optimization_coordinator.async_request_refresh()
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable optimization. The 15-min scheduler keeps running in the background."""
        _LOGGER.info("Disabling battery optimization")
        self._is_on = False
        self._optimization_coordinator.optimization_enabled = False
        self.async_write_ha_state()

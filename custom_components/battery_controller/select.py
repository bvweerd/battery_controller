"""Select platform for Battery Controller integration."""

from __future__ import annotations

import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONTROL_MODES,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Battery Controller select entities from a config entry."""
    data = entry.runtime_data
    device = data["device"]
    optimization_coordinator = data["optimization_coordinator"]

    entities = [
        BatteryControlModeSelect(hass, entry, device, optimization_coordinator),
    ]

    async_add_entities(entities)


class BatteryControlModeSelect(SelectEntity):
    """Select entity for battery control mode."""

    _attr_has_entity_name = True
    _attr_translation_key = "control_mode"
    _attr_name = "Control Mode"
    _attr_icon = "mdi:tune-variant"
    _attr_options = CONTROL_MODES

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        device: DeviceInfo,
        optimization_coordinator,
    ):
        """Initialize the select entity."""
        self.hass = hass
        self._entry = entry
        self._attr_device_info = device
        self._attr_unique_id = f"{entry.entry_id}_control_mode"
        self._optimization_coordinator = optimization_coordinator

    @property
    def current_option(self) -> str:
        """Return the current control mode."""
        return self._optimization_coordinator.control_mode

    async def async_select_option(self, option: str) -> None:
        """Set the control mode."""
        if option not in CONTROL_MODES:
            _LOGGER.warning("Invalid control mode: %s", option)
            return

        _LOGGER.info("Setting control mode to: %s", option)
        self._optimization_coordinator.control_mode = option

        # Persist control mode to config entry
        from .const import CONF_CONTROL_MODE

        new_data = {
            **self._entry.data,
            **self._entry.options,
            CONF_CONTROL_MODE: option,
        }
        self.hass.config_entries.async_update_entry(self._entry, options=new_data)

        # Trigger re-optimization with new mode
        await self._optimization_coordinator.async_request_refresh()
        self.async_write_ha_state()

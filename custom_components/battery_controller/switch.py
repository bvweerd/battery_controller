"""Switch platform for Battery Controller integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.const import EntityCategory
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .coordinator import OptimizationCoordinator

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Battery Controller switch entities from a config entry."""
    data = entry.runtime_data
    if data is None:
        _LOGGER.warning(
            "Skipping switch setup for %s: runtime_data missing", entry.entry_id
        )
        return

    device = data.device
    optimization_coordinator = data.optimization_coordinator
    if device is None or optimization_coordinator is None:
        _LOGGER.warning(
            "Skipping switch setup for %s: incomplete runtime_data", entry.entry_id
        )
        return

    entities = [
        BatteryOptimizationSwitch(hass, entry, device, optimization_coordinator),
    ]

    async_add_entities(entities)


class BatteryOptimizationSwitch(
    CoordinatorEntity[OptimizationCoordinator], RestoreEntity, SwitchEntity
):
    """Switch to enable/disable battery optimization.

    State is restored from the recorder on restart so the enabled/disabled
    setting survives HA restarts without requiring a config entry reload.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "optimization_enabled"
    _attr_name = "Optimization Enabled"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        device: DeviceInfo,
        optimization_coordinator: OptimizationCoordinator,
    ):
        """Initialize the switch entity."""
        super().__init__(optimization_coordinator)
        self.hass = hass
        self._entry = entry
        self._attr_device_info = device
        self._attr_unique_id = f"{entry.entry_id}_optimization_enabled"
        self._is_on = True

    async def async_added_to_hass(self) -> None:
        """Restore last known state on startup."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state is not None:
            self._is_on = last_state.state == STATE_ON
        # Sync the coordinator with the restored state
        self.coordinator.optimization_enabled = self._is_on

    @property
    def is_on(self) -> bool:
        """Return true if optimization is enabled."""
        return self._is_on

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable optimization and immediately run a fresh cycle."""
        _LOGGER.info("Enabling battery optimization")
        self._is_on = True
        self.coordinator.optimization_enabled = True
        await self.coordinator.async_request_refresh()
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable optimization. The 15-min scheduler keeps running in the background."""
        _LOGGER.info("Disabling battery optimization")
        self._is_on = False
        self.coordinator.optimization_enabled = False
        self.async_write_ha_state()

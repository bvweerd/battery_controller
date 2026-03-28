"""Number platform for Battery Controller integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_DEGRADATION_COST_PER_CYCLE,
    CONF_MANUAL_POWER_SETPOINT_W,
    CONF_MAX_CHARGE_POWER_KW,
    CONF_MAX_DISCHARGE_POWER_KW,
    CONF_MIN_PRICE_SPREAD,
    CONF_ZERO_GRID_DEADBAND_W,
    DEFAULT_DEGRADATION_COST_PER_CYCLE,
    DEFAULT_MANUAL_POWER_SETPOINT_W,
    DEFAULT_MAX_CHARGE_POWER_KW,
    DEFAULT_MAX_DISCHARGE_POWER_KW,
    DEFAULT_MIN_PRICE_SPREAD,
    DEFAULT_ZERO_GRID_DEADBAND_W,
)

_LOGGER = logging.getLogger(__name__)

# Number entities are not polled; state is set by the user or coordinator.
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Battery Controller number entities from a config entry."""
    data = entry.runtime_data
    config = data.config
    device = data.device

    entities = [
        DegradationCostNumber(hass, entry, device, config),
        MinPriceSpreadNumber(hass, entry, device, config),
        ZeroGridDeadbandNumber(hass, entry, device, config),
        ManualPowerSetpointNumber(hass, entry, device, config),
    ]

    async_add_entities(entities)


class BatteryControllerNumber(NumberEntity):
    """Base class for Battery Controller number entities."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _conf_key: (
        str  # Config entry key used by async_set_native_value; set in each subclass
    )

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        device: DeviceInfo,
        config: dict[str, Any],
        key: str,
    ):
        """Initialize the number entity."""
        self.hass = hass
        self._entry = entry
        self._attr_device_info = device
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._config = config
        self._key = key

    def _get_runtime_value(self, key: str, default: float) -> float:
        """Get runtime value from config entry options or data."""
        return float(self._entry.options.get(key, self._entry.data.get(key, default)))

    async def _set_runtime_value(self, key: str, value: float) -> None:
        """Set a runtime value in the config entry options."""
        self.hass.config_entries.async_update_entry(
            self._entry, options={**self._entry.options, key: value}
        )

    async def async_set_native_value(self, value: float) -> None:
        await self._set_runtime_value(self._conf_key, value)
        self.async_write_ha_state()


class DegradationCostNumber(BatteryControllerNumber):
    """Number entity for degradation cost per cycle."""

    _conf_key = CONF_DEGRADATION_COST_PER_CYCLE
    _attr_translation_key = "degradation_cost"
    _attr_name = "Degradation Cost"
    _attr_native_min_value = 0.0
    _attr_native_max_value = 1.00
    _attr_native_step = 0.01
    _attr_native_unit_of_measurement = "EUR/cycle"
    _attr_mode = NumberMode.BOX

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        device: DeviceInfo,
        config: dict[str, Any],
    ) -> None:
        super().__init__(hass, entry, device, config, "degradation_cost")

    @property
    def native_value(self) -> float:
        return self._get_runtime_value(
            CONF_DEGRADATION_COST_PER_CYCLE, DEFAULT_DEGRADATION_COST_PER_CYCLE
        )


class MinPriceSpreadNumber(BatteryControllerNumber):
    """Number entity for minimum price spread."""

    _conf_key = CONF_MIN_PRICE_SPREAD
    _attr_translation_key = "min_price_spread"
    _attr_name = "Minimum Price Spread"
    _attr_native_min_value = 0.0
    _attr_native_max_value = 0.50
    _attr_native_step = 0.01
    _attr_native_unit_of_measurement = "EUR/kWh"
    _attr_mode = NumberMode.BOX

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        device: DeviceInfo,
        config: dict[str, Any],
    ) -> None:
        super().__init__(hass, entry, device, config, "min_price_spread")

    @property
    def native_value(self) -> float:
        return self._get_runtime_value(CONF_MIN_PRICE_SPREAD, DEFAULT_MIN_PRICE_SPREAD)


class ZeroGridDeadbandNumber(BatteryControllerNumber):
    """Number entity for zero-grid deadband."""

    _conf_key = CONF_ZERO_GRID_DEADBAND_W
    _attr_translation_key = "zero_grid_deadband"
    _attr_name = "Zero Grid Deadband"
    _attr_native_min_value = 0.0
    _attr_native_max_value = 500.0
    _attr_native_step = 10.0
    _attr_native_unit_of_measurement = "W"
    _attr_mode = NumberMode.BOX

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        device: DeviceInfo,
        config: dict[str, Any],
    ) -> None:
        super().__init__(hass, entry, device, config, "zero_grid_deadband")

    @property
    def native_value(self) -> float:
        return self._get_runtime_value(
            CONF_ZERO_GRID_DEADBAND_W, DEFAULT_ZERO_GRID_DEADBAND_W
        )


class ManualPowerSetpointNumber(BatteryControllerNumber):
    """Number entity for the manual battery power setpoint.

    Active only in 'manual' control mode. Positive = discharge, negative = charge.
    Matches the sensor convention: the battery setpoint sensor mirrors this value.
    SoC limits and power limits are still respected.
    """

    _conf_key = CONF_MANUAL_POWER_SETPOINT_W
    _attr_translation_key = "manual_power_setpoint"
    _attr_name = "Manual Power Setpoint"
    _attr_native_step = 100.0
    _attr_native_unit_of_measurement = "W"
    _attr_mode = NumberMode.BOX

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        device: DeviceInfo,
        config: dict[str, Any],
    ) -> None:
        super().__init__(hass, entry, device, config, "manual_power_setpoint")

    @property
    def native_min_value(self) -> float:
        max_charge_kw = float(
            self._config.get(CONF_MAX_CHARGE_POWER_KW, DEFAULT_MAX_CHARGE_POWER_KW)
        )
        return -max_charge_kw * 1000

    @property
    def native_max_value(self) -> float:
        max_discharge_kw = float(
            self._config.get(
                CONF_MAX_DISCHARGE_POWER_KW, DEFAULT_MAX_DISCHARGE_POWER_KW
            )
        )
        return max_discharge_kw * 1000

    @property
    def native_value(self) -> float:
        return self._get_runtime_value(
            CONF_MANUAL_POWER_SETPOINT_W, DEFAULT_MANUAL_POWER_SETPOINT_W
        )

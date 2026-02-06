"""Number platform for Heating Curve Optimizer."""

from __future__ import annotations

import logging

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    CONF_INDOOR_TEMP_HYSTERESIS,
    CONF_INDOOR_TEMP_HYSTERESIS_LOWER,
    CONF_INDOOR_TEMP_HYSTERESIS_UPPER,
    CONF_TARGET_INDOOR_TEMP,
    DEFAULT_INDOOR_TEMP_HYSTERESIS,
    DEFAULT_INDOOR_TEMP_HYSTERESIS_LOWER,
    DEFAULT_INDOOR_TEMP_HYSTERESIS_UPPER,
    DEFAULT_TARGET_INDOOR_TEMP,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up number entities from a config entry."""
    device = DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name="Heating Curve Optimizer",
        manufacturer="Heating Curve Optimizer",
        model="Virtual",
    )

    config = {**entry.data, **entry.options}

    # Get initial values with fallback to legacy symmetric hysteresis
    legacy_hysteresis = config.get(
        CONF_INDOOR_TEMP_HYSTERESIS, DEFAULT_INDOOR_TEMP_HYSTERESIS
    )
    initial_lower = config.get(CONF_INDOOR_TEMP_HYSTERESIS_LOWER, legacy_hysteresis)
    initial_upper = config.get(CONF_INDOOR_TEMP_HYSTERESIS_UPPER, legacy_hysteresis)

    # Use defaults if legacy was also not set
    if initial_lower == DEFAULT_INDOOR_TEMP_HYSTERESIS:
        initial_lower = DEFAULT_INDOOR_TEMP_HYSTERESIS_LOWER
    if initial_upper == DEFAULT_INDOOR_TEMP_HYSTERESIS:
        initial_upper = DEFAULT_INDOOR_TEMP_HYSTERESIS_UPPER

    entities = [
        TargetIndoorTemperatureNumber(
            hass=hass,
            entry=entry,
            unique_id=f"{entry.entry_id}_target_indoor_temp",
            device=device,
            initial_value=config.get(
                CONF_TARGET_INDOOR_TEMP, DEFAULT_TARGET_INDOOR_TEMP
            ),
        ),
        IndoorTempHysteresisLowerNumber(
            hass=hass,
            entry=entry,
            unique_id=f"{entry.entry_id}_indoor_temp_hysteresis_lower",
            device=device,
            initial_value=initial_lower,
        ),
        IndoorTempHysteresisUpperNumber(
            hass=hass,
            entry=entry,
            unique_id=f"{entry.entry_id}_indoor_temp_hysteresis_upper",
            device=device,
            initial_value=initial_upper,
        ),
    ]

    async_add_entities(entities)


class BaseTemperatureNumber(NumberEntity, RestoreEntity):
    """Base class for temperature-related number entities."""

    _attr_has_entity_name = True
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_mode = NumberMode.SLIDER

    # Subclasses must define these
    _runtime_key: str = ""
    _log_name: str = ""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        unique_id: str,
        device: DeviceInfo,
        initial_value: float,
    ) -> None:
        """Initialize the number entity."""
        self.hass = hass
        self._entry = entry
        self._attr_unique_id = unique_id
        self._attr_device_info = device
        self._attr_native_value = initial_value

    async def async_added_to_hass(self) -> None:
        """Restore previous state on startup."""
        await super().async_added_to_hass()

        last_state = await self.async_get_last_state()
        if last_state is not None and last_state.state not in (
            "unknown",
            "unavailable",
        ):
            try:
                self._attr_native_value = float(last_state.state)
            except (ValueError, TypeError):
                pass

        self._update_runtime_data()

    async def async_set_native_value(self, value: float) -> None:
        """Update the current value."""
        self._attr_native_value = value
        self._update_runtime_data()
        self.async_write_ha_state()
        _LOGGER.debug("%s set to %.1f°C", self._log_name, value)

    def _update_runtime_data(self) -> None:
        """Update runtime data for use by other components."""
        self.hass.data.setdefault(DOMAIN, {}).setdefault("runtime", {})[
            self._runtime_key
        ] = self._attr_native_value


class TargetIndoorTemperatureNumber(BaseTemperatureNumber):
    """Number entity for target indoor temperature setpoint."""

    _attr_native_min_value = 15.0
    _attr_native_max_value = 25.0
    _attr_native_step = 0.5
    _attr_icon = "mdi:home-thermometer"
    _attr_translation_key = "target_indoor_temperature"

    _runtime_key = CONF_TARGET_INDOOR_TEMP
    _log_name = "Target indoor temperature"


class IndoorTempHysteresisLowerNumber(BaseTemperatureNumber):
    """Number entity for lower hysteresis (how far below target before heat pump ON)."""

    _attr_native_min_value = 0.1
    _attr_native_max_value = 2.0
    _attr_native_step = 0.1
    _attr_icon = "mdi:thermometer-chevron-down"
    _attr_translation_key = "indoor_temp_hysteresis_lower"

    _runtime_key = CONF_INDOOR_TEMP_HYSTERESIS_LOWER
    _log_name = "Lower hysteresis (heat pump ON)"


class IndoorTempHysteresisUpperNumber(BaseTemperatureNumber):
    """Number entity for upper hysteresis (how far above target before heat pump OFF)."""

    _attr_native_min_value = 0.1
    _attr_native_max_value = 2.0
    _attr_native_step = 0.1
    _attr_icon = "mdi:thermometer-chevron-up"
    _attr_translation_key = "indoor_temp_hysteresis_upper"

    _runtime_key = CONF_INDOOR_TEMP_HYSTERESIS_UPPER
    _log_name = "Upper hysteresis (heat pump OFF)"

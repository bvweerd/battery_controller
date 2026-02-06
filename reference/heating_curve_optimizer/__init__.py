# custom_components/heating_curve_optimizer/__init__.py

from __future__ import annotations

import asyncio
import logging

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.entity import DeviceInfo

from .const import DOMAIN, PLATFORMS
from .coordinator import (
    WeatherDataCoordinator,
    HeatCalculationCoordinator,
    OptimizationCoordinator,
)

_LOGGER = logging.getLogger(__name__)


CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the base integration (no YAML)."""
    hass.data.setdefault(DOMAIN, {})
    _LOGGER.info("Initialized Heating Curve Optimizer")
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a config entry by forwarding to sensor & number platforms."""
    _LOGGER.info("Setting up entry %s", entry.entry_id)

    # Ensure DOMAIN exists in hass.data
    hass.data.setdefault(DOMAIN, {})

    # Merge options and data for configuration
    config = {**entry.data, **entry.options}

    # Initialize coordinators
    _LOGGER.debug("Initializing coordinators for entry %s", entry.entry_id)

    # 1. Weather data coordinator (API calls to open-meteo)
    weather_coordinator = WeatherDataCoordinator(hass)
    await weather_coordinator.async_config_entry_first_refresh()

    # 2. Heat calculation coordinator (depends on weather coordinator)
    heat_coordinator = HeatCalculationCoordinator(hass, weather_coordinator, config)
    await heat_coordinator.async_setup()
    await heat_coordinator.async_config_entry_first_refresh()

    # 3. Optimization coordinator (depends on heat coordinator)
    optimization_coordinator = OptimizationCoordinator(hass, heat_coordinator, config)
    await optimization_coordinator.async_setup()

    # Trigger first optimization async (don't block startup)
    # This allows sensors to be available immediately while optimization runs in background
    async def _trigger_first_optimization():
        """Trigger first optimization after a short delay."""
        await asyncio.sleep(5)  # Give sensors time to initialize
        _LOGGER.info("Triggering first optimization run")
        await optimization_coordinator.async_request_refresh()

    hass.async_create_task(_trigger_first_optimization())

    # Create device info for all entities
    device = DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name="Heating Curve Optimizer",
        manufacturer="Custom",
        model="Dynamic Heating Optimizer",
        sw_version="2.0.0",
    )

    # Store coordinators and config in hass.data
    hass.data[DOMAIN][entry.entry_id] = {
        "weather_coordinator": weather_coordinator,
        "heat_coordinator": heat_coordinator,
        "optimization_coordinator": optimization_coordinator,
        "config": config,
        "entry": entry,
        "device": device,
    }

    _LOGGER.debug("Coordinators initialized successfully")

    entry.async_on_unload(entry.add_update_listener(_update_listener))

    # Forward entry to ALL our platforms in one call:
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    _LOGGER.debug("Forwarded entry %s to platforms %s", entry.entry_id, PLATFORMS)
    return True


async def _update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update by reloading the config entry."""
    _LOGGER.debug("Reloading config entry %s", entry.entry_id)
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry and its platforms."""
    _LOGGER.info("Unloading entry %s", entry.entry_id)

    # Shutdown coordinators
    entry_data = hass.data[DOMAIN].get(entry.entry_id)
    if entry_data:
        heat_coordinator = entry_data.get("heat_coordinator")
        if heat_coordinator:
            await heat_coordinator.async_shutdown()

        optimization_coordinator = entry_data.get("optimization_coordinator")
        if optimization_coordinator:
            await optimization_coordinator.async_shutdown()

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop("entities", None)
        hass.data[DOMAIN].pop(entry.entry_id, None)
        runtime = hass.data[DOMAIN].get("runtime")
        if runtime and entry.entry_id in runtime:
            runtime.pop(entry.entry_id, None)
            if not runtime:
                hass.data[DOMAIN].pop("runtime")
        _LOGGER.debug("Successfully unloaded entry %s", entry.entry_id)
        if not hass.data[DOMAIN]:
            hass.data.pop(DOMAIN)
    else:
        _LOGGER.warning("Failed to unload entry %s", entry.entry_id)
    return unload_ok

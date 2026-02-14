"""Battery Controller integration for Home Assistant."""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.entity import DeviceInfo

from .const import (
    DOMAIN,
    PLATFORMS,
)
from .coordinator import (
    WeatherDataCoordinator,
    ForecastCoordinator,
    OptimizationCoordinator,
)

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the base integration (no YAML)."""
    hass.data.setdefault(DOMAIN, {})
    _LOGGER.info("Initialized Battery Controller")
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a config entry by forwarding to sensor & number platforms."""
    _LOGGER.info("Setting up entry %s", entry.entry_id)

    # Ensure DOMAIN exists in hass.data
    hass.data.setdefault(DOMAIN, {})

    # Merge options and data for configuration; include entry_id for sensor lookups
    config = {**entry.data, **entry.options, "entry_id": entry.entry_id}

    # Initialize coordinators
    _LOGGER.debug("Initializing coordinators for entry %s", entry.entry_id)

    # 1. Weather data coordinator (API calls to open-meteo)
    weather_coordinator = WeatherDataCoordinator(hass)

    # 2. Forecast coordinator (depends on weather coordinator)
    forecast_coordinator = ForecastCoordinator(hass, weather_coordinator, config)
    await forecast_coordinator.async_setup()

    # 3. Optimization coordinator (depends on forecast coordinator)
    optimization_coordinator = OptimizationCoordinator(
        hass, weather_coordinator, forecast_coordinator, config
    )
    await optimization_coordinator.async_setup()

    # Create device info for all entities
    device = DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name="Battery Controller",
        manufacturer="Custom",
        model="Battery Optimization Controller",
        sw_version="1.0.0",
    )

    # Store coordinators and config in runtime_data
    entry.runtime_data = {
        "weather_coordinator": weather_coordinator,
        "forecast_coordinator": forecast_coordinator,
        "optimization_coordinator": optimization_coordinator,
        "config": config,
        "device": device,
    }

    _LOGGER.debug("Coordinators initialized successfully")

    entry.async_on_unload(entry.add_update_listener(_update_listener))

    # Forward entry to ALL our platforms in one call
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
    if hasattr(entry, "runtime_data"):
        forecast_coordinator = entry.runtime_data.get("forecast_coordinator")
        if forecast_coordinator:
            await forecast_coordinator.async_shutdown()

        optimization_coordinator = entry.runtime_data.get("optimization_coordinator")
        if optimization_coordinator:
            await optimization_coordinator.async_shutdown()

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        _LOGGER.debug("Successfully unloaded entry %s", entry.entry_id)

    return unload_ok

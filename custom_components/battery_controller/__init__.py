"""Battery Controller integration for Home Assistant."""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.entity import DeviceInfo

from .const import (
    CONF_PV_EXTRA_ARRAYS,
    CONF_PV2_PEAK_POWER_KWP,
    CONF_PV2_ORIENTATION,
    CONF_PV2_TILT,
    CONF_PV3_PEAK_POWER_KWP,
    CONF_PV3_ORIENTATION,
    CONF_PV3_TILT,
    DEFAULT_PV2_ORIENTATION,
    DEFAULT_PV2_PEAK_POWER_KWP,
    DEFAULT_PV2_TILT,
    DEFAULT_PV3_ORIENTATION,
    DEFAULT_PV3_PEAK_POWER_KWP,
    DEFAULT_PV3_TILT,
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


async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Migrate old config entries to new format."""
    if config_entry.version == 1:
        _LOGGER.info("Migrating config entry from version 1 to 2")
        new_data = {**config_entry.data}

        # Convert legacy pv2/pv3 keys to pv_extra_arrays list
        extra_arrays: list[dict] = []

        pv2_kwp = float(new_data.pop(CONF_PV2_PEAK_POWER_KWP, DEFAULT_PV2_PEAK_POWER_KWP))
        pv2_orient = float(new_data.pop(CONF_PV2_ORIENTATION, DEFAULT_PV2_ORIENTATION))
        pv2_tilt = float(new_data.pop(CONF_PV2_TILT, DEFAULT_PV2_TILT))
        if pv2_kwp > 0:
            extra_arrays.append(
                {
                    "peak_power_kwp": pv2_kwp,
                    "orientation": pv2_orient,
                    "tilt": pv2_tilt,
                    "dc_coupled": False,
                }
            )

        pv3_kwp = float(new_data.pop(CONF_PV3_PEAK_POWER_KWP, DEFAULT_PV3_PEAK_POWER_KWP))
        pv3_orient = float(new_data.pop(CONF_PV3_ORIENTATION, DEFAULT_PV3_ORIENTATION))
        pv3_tilt = float(new_data.pop(CONF_PV3_TILT, DEFAULT_PV3_TILT))
        if pv3_kwp > 0:
            extra_arrays.append(
                {
                    "peak_power_kwp": pv3_kwp,
                    "orientation": pv3_orient,
                    "tilt": pv3_tilt,
                    "dc_coupled": False,
                }
            )

        new_data[CONF_PV_EXTRA_ARRAYS] = extra_arrays

        hass.config_entries.async_update_entry(
            config_entry, data=new_data, version=2
        )
        _LOGGER.info(
            "Migration complete: converted %d legacy PV arrays to pv_extra_arrays",
            len(extra_arrays),
        )

    return True


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

    # Merge options and data for configuration
    config = {**entry.data, **entry.options}

    # Initialize coordinators
    _LOGGER.debug("Initializing coordinators for entry %s", entry.entry_id)

    # 1. Weather data coordinator (API calls to open-meteo)
    weather_coordinator = WeatherDataCoordinator(hass)
    await weather_coordinator.async_config_entry_first_refresh()

    # 2. Forecast coordinator (depends on weather coordinator)
    forecast_coordinator = ForecastCoordinator(hass, weather_coordinator, config)
    await forecast_coordinator.async_setup()
    await forecast_coordinator.async_config_entry_first_refresh()

    # 3. Optimization coordinator (depends on forecast coordinator)
    optimization_coordinator = OptimizationCoordinator(
        hass, weather_coordinator, forecast_coordinator, config
    )
    await optimization_coordinator.async_setup()
    await optimization_coordinator.async_config_entry_first_refresh()

    # Create device info for all entities
    device = DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name="Battery Controller",
        manufacturer="Custom",
        model="Battery Optimization Controller",
        sw_version="1.0.0",
    )

    # Store coordinators and config in hass.data
    hass.data[DOMAIN][entry.entry_id] = {
        "weather_coordinator": weather_coordinator,
        "forecast_coordinator": forecast_coordinator,
        "optimization_coordinator": optimization_coordinator,
        "config": config,
        "entry": entry,
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
    entry_data = hass.data[DOMAIN].get(entry.entry_id)
    if entry_data:
        forecast_coordinator = entry_data.get("forecast_coordinator")
        if forecast_coordinator:
            await forecast_coordinator.async_shutdown()

        optimization_coordinator = entry_data.get("optimization_coordinator")
        if optimization_coordinator:
            await optimization_coordinator.async_shutdown()

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        _LOGGER.debug("Successfully unloaded entry %s", entry.entry_id)
        if not hass.data[DOMAIN]:
            hass.data.pop(DOMAIN)
    else:
        _LOGGER.warning("Failed to unload entry %s", entry.entry_id)

    return unload_ok

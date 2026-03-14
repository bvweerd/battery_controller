"""Battery Controller integration for Home Assistant."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.entity import DeviceInfo

from .const import (
    BATTERY_SUBENTRY_TYPE,
    CONF_CAPACITY_KWH,
    CONF_CONTROL_MODE,
    CONF_DEGRADATION_COST_PER_KWH,
    CONF_MANUAL_POWER_SETPOINT_W,
    CONF_MAX_CHARGE_POWER_KW,
    CONF_MAX_DISCHARGE_POWER_KW,
    CONF_MIN_PRICE_SPREAD,
    CONF_NAME,
    CONF_PV_DC_COUPLED,
    CONF_PV_DC_PEAK_POWER_KWP,
    CONF_ZERO_GRID_DEADBAND_W,
    DEFAULT_MAX_CHARGE_POWER_KW,
    DEFAULT_MAX_DISCHARGE_POWER_KW,
    DOMAIN,
    PLATFORMS,
    PV_SUBENTRY_TYPE,
)
from .coordinator import (
    WeatherDataCoordinator,
    ForecastCoordinator,
    OptimizationCoordinator,
)

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

# Load version once at module import time (blocking I/O at module level is fine).
_MANIFEST: dict[str, Any] = json.loads(
    (Path(__file__).parent / "manifest.json").read_text(encoding="utf-8")
)

# Keys stored in entry.options by number/select/switch entities that do NOT
# require a full reload of the integration when they change.  Everything else
# (sensor IDs, battery hardware specs, timing parameters) triggers a reload so
# the coordinators are re-initialised with the new structural configuration.
_NO_RELOAD_KEYS = frozenset(
    {
        CONF_DEGRADATION_COST_PER_KWH,
        CONF_MIN_PRICE_SPREAD,
        CONF_ZERO_GRID_DEADBAND_W,
        CONF_MANUAL_POWER_SETPOINT_W,
        CONF_CONTROL_MODE,
    }
)


@dataclass
class BatteryControllerData:
    """Runtime data stored on the config entry."""

    weather_coordinator: WeatherDataCoordinator
    forecast_coordinator: ForecastCoordinator
    optimization_coordinator: OptimizationCoordinator
    config: dict[str, Any]
    device: DeviceInfo
    battery_devices: dict[str, DeviceInfo]  # keyed by subentry_id
    pv_devices: dict[str, DeviceInfo]  # keyed by subentry_id


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a config entry by forwarding to sensor & number platforms."""
    _LOGGER.info("Setting up entry %s", entry.entry_id)

    # Collect subentries by type
    pv_arrays = [
        {**s.data, "subentry_id": s.subentry_id}
        for s in entry.subentries.values()
        if s.subentry_type == PV_SUBENTRY_TYPE
    ]
    battery_subentries = [
        (s.subentry_id, dict(s.data))
        for s in entry.subentries.values()
        if s.subentry_type == BATTERY_SUBENTRY_TYPE
    ]

    # Derive DC-coupling summary from PV arrays
    pv_dc_coupled = any(bool(a.get("dc_coupled")) for a in pv_arrays)
    pv_dc_total_kwp = sum(
        float(a.get("peak_power_kwp", 0)) for a in pv_arrays if a.get("dc_coupled")
    )

    # Derive combined battery limits for number entity ranges
    combined_max_charge_kw = (
        sum(
            float(d.get(CONF_MAX_CHARGE_POWER_KW, DEFAULT_MAX_CHARGE_POWER_KW))
            for _, d in battery_subentries
        )
        or DEFAULT_MAX_CHARGE_POWER_KW
    )
    combined_max_discharge_kw = (
        sum(
            float(d.get(CONF_MAX_DISCHARGE_POWER_KW, DEFAULT_MAX_DISCHARGE_POWER_KW))
            for _, d in battery_subentries
        )
        or DEFAULT_MAX_DISCHARGE_POWER_KW
    )

    # Merge options and data for configuration; include entry_id for sensor lookups
    config = {
        **entry.data,
        **entry.options,
        "entry_id": entry.entry_id,
        "pv_arrays": pv_arrays,
        "battery_subentries": battery_subentries,
        CONF_PV_DC_COUPLED: pv_dc_coupled,
        CONF_PV_DC_PEAK_POWER_KWP: pv_dc_total_kwp,
        CONF_MAX_CHARGE_POWER_KW: combined_max_charge_kw,
        CONF_MAX_DISCHARGE_POWER_KW: combined_max_discharge_kw,
    }

    _LOGGER.debug("Initializing coordinators for entry %s", entry.entry_id)

    # 1. Weather data coordinator (API calls to open-meteo)
    weather_coordinator = WeatherDataCoordinator(hass)
    await weather_coordinator.async_config_entry_first_refresh()

    # 2. Forecast coordinator (depends on weather coordinator)
    forecast_coordinator = ForecastCoordinator(hass, weather_coordinator, config)
    await forecast_coordinator.async_setup()
    await forecast_coordinator.async_refresh()

    # 3. Optimization coordinator (depends on forecast coordinator)
    optimization_coordinator = OptimizationCoordinator(
        hass, weather_coordinator, forecast_coordinator, config
    )
    await optimization_coordinator.async_setup()
    await optimization_coordinator.async_refresh()

    device = DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name="Battery Controller",
        manufacturer="Custom",
        model="Battery Optimization Controller",
        sw_version=_MANIFEST.get("version", "unknown"),
    )

    battery_devices: dict[str, DeviceInfo] = {}
    for subentry_id, subentry_data in battery_subentries:
        subentry = entry.subentries.get(subentry_id)
        title = (
            subentry.title
            if subentry is not None
            else subentry_data.get(CONF_NAME)
            or f"{subentry_data.get(CONF_CAPACITY_KWH, '?')} kWh"
        )
        capacity_kwh = subentry_data.get(CONF_CAPACITY_KWH, "?")
        battery_devices[subentry_id] = DeviceInfo(
            identifiers={(DOMAIN, subentry_id)},
            name=title,
            manufacturer="Custom",
            model=f"{capacity_kwh} kWh Battery",
            via_device=(DOMAIN, entry.entry_id),
        )

    pv_devices: dict[str, DeviceInfo] = {}
    for s in entry.subentries.values():
        if s.subentry_type == PV_SUBENTRY_TYPE:
            kwp = s.data.get("peak_power_kwp", "?")
            pv_devices[s.subentry_id] = DeviceInfo(
                identifiers={(DOMAIN, s.subentry_id)},
                name=s.title,
                manufacturer="Custom",
                model=f"{kwp} kWp PV Array",
                via_device=(DOMAIN, entry.entry_id),
            )

    entry.runtime_data = BatteryControllerData(
        weather_coordinator=weather_coordinator,
        forecast_coordinator=forecast_coordinator,
        optimization_coordinator=optimization_coordinator,
        config=config,
        device=device,
        battery_devices=battery_devices,
        pv_devices=pv_devices,
    )

    _LOGGER.debug("Coordinators initialized successfully")

    entry.async_on_unload(entry.add_update_listener(_update_listener))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    _LOGGER.debug("Forwarded entry %s to platforms %s", entry.entry_id, PLATFORMS)
    return True


async def _update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update.

    Only reloads the integration when a structural config key changes.
    Runtime-tunable keys (SoC limits, degradation cost, manual setpoint, control
    mode) are re-read live by the coordinator and do not require a reload.
    """
    if entry.runtime_data is None:
        return

    # Snapshot from setup: coordinator.config was built as entry.data | entry.options
    old_snapshot: dict[str, Any] = entry.runtime_data.config

    needs_reload = any(
        key not in _NO_RELOAD_KEYS and old_snapshot.get(key) != val
        for key, val in entry.options.items()
    )

    if needs_reload:
        _LOGGER.debug("Structural config changed; reloading entry %s", entry.entry_id)
        await hass.config_entries.async_reload(entry.entry_id)
    else:
        _LOGGER.debug(
            "Runtime-only options changed; skipping reload for entry %s",
            entry.entry_id,
        )


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry and its platforms."""
    _LOGGER.info("Unloading entry %s", entry.entry_id)

    if entry.runtime_data is not None:
        await entry.runtime_data.forecast_coordinator.async_shutdown()
        await entry.runtime_data.optimization_coordinator.async_shutdown()
        await entry.runtime_data.weather_coordinator.async_shutdown()

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        _LOGGER.debug("Successfully unloaded entry %s", entry.entry_id)

    return unload_ok

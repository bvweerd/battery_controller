"""Battery Controller integration for Home Assistant."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.entity import DeviceInfo

from .const import (
    CONF_CONTROL_MODE,
    CONF_DEGRADATION_COST_PER_KWH,
    CONF_MANUAL_POWER_SETPOINT_W,
    CONF_MAX_SOC_PERCENT,
    CONF_MIN_SOC_PERCENT,
    CONF_MIN_PRICE_SPREAD,
    CONF_PV_DC_COUPLED,
    CONF_PV_DC_PEAK_POWER_KWP,
    CONF_ZERO_GRID_DEADBAND_W,
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
        CONF_MIN_SOC_PERCENT,
        CONF_MAX_SOC_PERCENT,
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


async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Migrate old config entry versions."""
    _LOGGER.info(
        "Migrating Battery Controller entry from version %s", config_entry.version
    )

    if config_entry.version < 3:
        old_data = {**config_entry.data}
        pv_arrays_to_create: list[tuple[str, dict]] = []

        # Primary AC array (if configured)
        kwp = float(old_data.get("pv_peak_power_kwp", 0.0))
        if kwp > 0:
            pv_arrays_to_create.append(
                (
                    f"{kwp} kWp AC",
                    {
                        "peak_power_kwp": kwp,
                        "orientation": float(old_data.get("pv_orientation", 180.0)),
                        "tilt": float(old_data.get("pv_tilt", 35.0)),
                        "efficiency_factor": float(
                            old_data.get("pv_efficiency_factor", 0.85)
                        ),
                        "dc_coupled": False,
                    },
                )
            )

        # Primary DC-coupled array (if configured separately)
        dc_kwp = float(old_data.get("pv_dc_peak_power_kwp", 0.0))
        if old_data.get("pv_dc_coupled") and dc_kwp > 0:
            pv_arrays_to_create.append(
                (
                    f"{dc_kwp} kWp DC",
                    {
                        "peak_power_kwp": dc_kwp,
                        "orientation": float(old_data.get("pv_orientation", 180.0)),
                        "tilt": float(old_data.get("pv_tilt", 35.0)),
                        "efficiency_factor": float(
                            old_data.get("pv_dc_efficiency", 0.97)
                        ),
                        "dc_coupled": True,
                    },
                )
            )

        # Extra arrays from old pv_extra_arrays list
        for arr in old_data.get("pv_extra_arrays", []):
            arr_kwp = float(arr.get("peak_power_kwp", 0.0))
            if arr_kwp <= 0:
                continue
            dc = bool(arr.get("dc_coupled", False))
            coupling = "DC" if dc else "AC"
            pv_arrays_to_create.append(
                (
                    f"{arr_kwp} kWp {coupling}",
                    {
                        "peak_power_kwp": arr_kwp,
                        "orientation": float(arr.get("orientation", 180.0)),
                        "tilt": float(arr.get("tilt", 35.0)),
                        "efficiency_factor": float(arr.get("efficiency_factor", 0.85)),
                        "dc_coupled": dc,
                    },
                )
            )

        for title, data in pv_arrays_to_create:
            subentry = ConfigSubentry(
                subentry_type=PV_SUBENTRY_TYPE,
                title=title,
                data=MappingProxyType(data),
                unique_id=None,
            )
            hass.config_entries.async_add_subentry(config_entry, subentry)

        # Strip PV-specific fields from main config; keep pv_dc_efficiency
        pv_keys_to_remove = {
            "pv_peak_power_kwp",
            "pv_orientation",
            "pv_tilt",
            "pv_efficiency_factor",
            "pv_dc_coupled",
            "pv_dc_peak_power_kwp",
            "pv_extra_arrays",
        }
        new_data = {k: v for k, v in old_data.items() if k not in pv_keys_to_remove}
        hass.config_entries.async_update_entry(config_entry, data=new_data, version=3)
        _LOGGER.info(
            "Migration to v3 complete: %d PV subentries created",
            len(pv_arrays_to_create),
        )

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up a config entry by forwarding to sensor & number platforms."""
    _LOGGER.info("Setting up entry %s", entry.entry_id)

    # Collect PV arrays from subentries and derive DC-coupling summary
    pv_arrays = [
        s.data for s in entry.subentries.values() if s.subentry_type == PV_SUBENTRY_TYPE
    ]
    pv_dc_coupled = any(bool(a.get("dc_coupled")) for a in pv_arrays)
    pv_dc_total_kwp = sum(
        float(a.get("peak_power_kwp", 0)) for a in pv_arrays if a.get("dc_coupled")
    )

    # Merge options and data for configuration; include entry_id for sensor lookups
    config = {
        **entry.data,
        **entry.options,
        "entry_id": entry.entry_id,
        "pv_arrays": pv_arrays,
        CONF_PV_DC_COUPLED: pv_dc_coupled,
        CONF_PV_DC_PEAK_POWER_KWP: pv_dc_total_kwp,
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

    entry.runtime_data = BatteryControllerData(
        weather_coordinator=weather_coordinator,
        forecast_coordinator=forecast_coordinator,
        optimization_coordinator=optimization_coordinator,
        config=config,
        device=device,
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

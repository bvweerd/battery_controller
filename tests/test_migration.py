"""Tests for Battery Controller config entry migration (v2 → v3)."""

from __future__ import annotations

import pytest
from homeassistant import config_entries, setup
from homeassistant.core import HomeAssistant

from custom_components.battery_controller.const import DOMAIN, PV_SUBENTRY_TYPE
from custom_components.battery_controller import async_migrate_entry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_BASE_SENSORS = {
    "price_sensor": "sensor.nordpool",
    "battery_soc_sensor": "sensor.soc",
}

_BASE_BATTERY = {
    "capacity_kwh": 10.0,
    "max_charge_power_kw": 5.0,
    "max_discharge_power_kw": 5.0,
    "round_trip_efficiency": 0.9,
    "pv_dc_efficiency": 0.97,
}


def _make_v2_entry(hass: HomeAssistant, data: dict) -> config_entries.ConfigEntry:
    """Create a VERSION=2 config entry and register it in hass."""
    entry = config_entries.ConfigEntry(
        entry_id="migrate_test",
        domain=DOMAIN,
        title="Battery Controller",
        data={**_BASE_BATTERY, **_BASE_SENSORS, **data},
        options={},
        source="user",
        version=2,
        minor_version=1,
        unique_id=DOMAIN,
        discovery_keys=set(),
        subentries_data=None,
    )
    hass.config_entries._entries[entry.entry_id] = entry
    return entry


@pytest.fixture(autouse=True)
async def setup_ha(hass: HomeAssistant) -> None:
    """Set up Home Assistant for testing."""
    await setup.async_setup_component(hass, "persistent_notification", {})


# ---------------------------------------------------------------------------
# Migration: v2 → v3
# ---------------------------------------------------------------------------


async def test_migrate_v2_no_pv(hass: HomeAssistant) -> None:
    """v2 entry with no PV fields migrates to v3 with no subentries."""
    entry = _make_v2_entry(
        hass,
        {
            "pv_peak_power_kwp": 0.0,
            "pv_dc_coupled": False,
            "pv_dc_peak_power_kwp": 0.0,
            "pv_extra_arrays": [],
        },
    )
    result = await async_migrate_entry(hass, entry)
    assert result is True
    assert entry.version == 3
    assert len(entry.subentries) == 0


async def test_migrate_v2_primary_pv_only(hass: HomeAssistant) -> None:
    """v2 entry with primary AC array → one AC subentry."""
    entry = _make_v2_entry(
        hass,
        {
            "pv_peak_power_kwp": 6.0,
            "pv_orientation": 180.0,
            "pv_tilt": 35.0,
            "pv_efficiency_factor": 0.85,
            "pv_dc_coupled": False,
            "pv_dc_peak_power_kwp": 0.0,
            "pv_extra_arrays": [],
        },
    )
    result = await async_migrate_entry(hass, entry)
    assert result is True
    assert entry.version == 3
    assert len(entry.subentries) == 1

    sub = list(entry.subentries.values())[0]
    assert sub.subentry_type == PV_SUBENTRY_TYPE
    assert sub.data["peak_power_kwp"] == 6.0
    assert sub.data["orientation"] == 180.0
    assert sub.data["tilt"] == 35.0
    assert sub.data["efficiency_factor"] == 0.85
    assert sub.data["dc_coupled"] is False


async def test_migrate_v2_primary_pv_zero_kwp(hass: HomeAssistant) -> None:
    """v2 entry with pv_peak_power_kwp=0.0 → no subentry created."""
    entry = _make_v2_entry(
        hass,
        {
            "pv_peak_power_kwp": 0.0,
            "pv_orientation": 180.0,
            "pv_tilt": 35.0,
            "pv_efficiency_factor": 0.85,
            "pv_dc_coupled": False,
            "pv_dc_peak_power_kwp": 0.0,
            "pv_extra_arrays": [],
        },
    )
    await async_migrate_entry(hass, entry)
    assert len(entry.subentries) == 0


async def test_migrate_v2_dc_coupled_primary(hass: HomeAssistant) -> None:
    """v2 entry with pv_dc_coupled=True and dc peak power → one DC subentry."""
    entry = _make_v2_entry(
        hass,
        {
            "pv_peak_power_kwp": 0.0,
            "pv_orientation": 180.0,
            "pv_tilt": 35.0,
            "pv_efficiency_factor": 0.85,
            "pv_dc_coupled": True,
            "pv_dc_peak_power_kwp": 3.0,
            "pv_dc_efficiency": 0.97,
            "pv_extra_arrays": [],
        },
    )
    await async_migrate_entry(hass, entry)
    assert len(entry.subentries) == 1

    sub = list(entry.subentries.values())[0]
    assert sub.data["dc_coupled"] is True
    assert sub.data["peak_power_kwp"] == 3.0
    assert sub.data["efficiency_factor"] == 0.97


async def test_migrate_v2_dc_coupled_zero_kwp_skipped(hass: HomeAssistant) -> None:
    """pv_dc_coupled=True but pv_dc_peak_power_kwp=0 → no DC subentry."""
    entry = _make_v2_entry(
        hass,
        {
            "pv_peak_power_kwp": 0.0,
            "pv_dc_coupled": True,
            "pv_dc_peak_power_kwp": 0.0,
            "pv_extra_arrays": [],
        },
    )
    await async_migrate_entry(hass, entry)
    assert len(entry.subentries) == 0


async def test_migrate_v2_extra_arrays(hass: HomeAssistant) -> None:
    """v2 entry with pv_extra_arrays → one subentry per array."""
    entry = _make_v2_entry(
        hass,
        {
            "pv_peak_power_kwp": 0.0,
            "pv_dc_coupled": False,
            "pv_dc_peak_power_kwp": 0.0,
            "pv_extra_arrays": [
                {
                    "peak_power_kwp": 4.0,
                    "orientation": 90.0,
                    "tilt": 25.0,
                    "dc_coupled": False,
                },
                {
                    "peak_power_kwp": 2.5,
                    "orientation": 270.0,
                    "tilt": 30.0,
                    "dc_coupled": True,
                },
            ],
        },
    )
    await async_migrate_entry(hass, entry)
    assert len(entry.subentries) == 2

    dc_subs = [s for s in entry.subentries.values() if s.data["dc_coupled"]]
    ac_subs = [s for s in entry.subentries.values() if not s.data["dc_coupled"]]
    assert len(dc_subs) == 1
    assert len(ac_subs) == 1
    assert dc_subs[0].data["peak_power_kwp"] == 2.5
    assert ac_subs[0].data["peak_power_kwp"] == 4.0


async def test_migrate_v2_primary_plus_extras(hass: HomeAssistant) -> None:
    """v2 with primary AC + DC + 2 extra arrays → 4 subentries total."""
    entry = _make_v2_entry(
        hass,
        {
            "pv_peak_power_kwp": 6.0,
            "pv_orientation": 180.0,
            "pv_tilt": 35.0,
            "pv_efficiency_factor": 0.85,
            "pv_dc_coupled": True,
            "pv_dc_peak_power_kwp": 3.0,
            "pv_dc_efficiency": 0.97,
            "pv_extra_arrays": [
                {
                    "peak_power_kwp": 4.0,
                    "orientation": 90.0,
                    "tilt": 25.0,
                    "dc_coupled": False,
                },
                {
                    "peak_power_kwp": 2.0,
                    "orientation": 270.0,
                    "tilt": 20.0,
                    "dc_coupled": False,
                },
            ],
        },
    )
    await async_migrate_entry(hass, entry)
    assert len(entry.subentries) == 4


async def test_migrate_v2_extra_array_zero_kwp_skipped(hass: HomeAssistant) -> None:
    """Extra arrays with kwp=0 are skipped during migration."""
    entry = _make_v2_entry(
        hass,
        {
            "pv_peak_power_kwp": 0.0,
            "pv_dc_coupled": False,
            "pv_dc_peak_power_kwp": 0.0,
            "pv_extra_arrays": [
                {
                    "peak_power_kwp": 0.0,
                    "orientation": 180.0,
                    "tilt": 35.0,
                    "dc_coupled": False,
                },
                {
                    "peak_power_kwp": 4.0,
                    "orientation": 90.0,
                    "tilt": 25.0,
                    "dc_coupled": False,
                },
            ],
        },
    )
    await async_migrate_entry(hass, entry)
    # Only the non-zero array should become a subentry
    assert len(entry.subentries) == 1
    sub = list(entry.subentries.values())[0]
    assert sub.data["peak_power_kwp"] == 4.0


async def test_migrate_v2_pv_fields_removed_from_data(hass: HomeAssistant) -> None:
    """After migration, PV array fields are no longer in entry.data."""
    entry = _make_v2_entry(
        hass,
        {
            "pv_peak_power_kwp": 6.0,
            "pv_orientation": 180.0,
            "pv_tilt": 35.0,
            "pv_efficiency_factor": 0.85,
            "pv_dc_coupled": False,
            "pv_dc_peak_power_kwp": 0.0,
            "pv_extra_arrays": [],
        },
    )
    await async_migrate_entry(hass, entry)

    removed = {
        "pv_peak_power_kwp",
        "pv_orientation",
        "pv_tilt",
        "pv_efficiency_factor",
        "pv_dc_coupled",
        "pv_dc_peak_power_kwp",
        "pv_extra_arrays",
    }
    for key in removed:
        assert key not in entry.data, f"Expected {key!r} removed from entry.data"


async def test_migrate_v2_pv_dc_efficiency_kept_in_data(hass: HomeAssistant) -> None:
    """pv_dc_efficiency stays in entry.data after migration (battery model needs it)."""
    entry = _make_v2_entry(
        hass,
        {
            "pv_peak_power_kwp": 0.0,
            "pv_dc_coupled": True,
            "pv_dc_peak_power_kwp": 3.0,
            "pv_dc_efficiency": 0.97,
            "pv_extra_arrays": [],
        },
    )
    await async_migrate_entry(hass, entry)
    assert entry.data.get("pv_dc_efficiency") == 0.97


async def test_migrate_v2_non_pv_data_preserved(hass: HomeAssistant) -> None:
    """Battery and sensor fields survive migration unchanged."""
    entry = _make_v2_entry(
        hass,
        {
            "pv_peak_power_kwp": 0.0,
            "pv_dc_coupled": False,
            "pv_dc_peak_power_kwp": 0.0,
            "pv_extra_arrays": [],
        },
    )
    await async_migrate_entry(hass, entry)

    assert entry.data["capacity_kwh"] == 10.0
    assert entry.data["round_trip_efficiency"] == 0.9
    assert entry.data["price_sensor"] == "sensor.nordpool"
    assert entry.data["battery_soc_sensor"] == "sensor.soc"
    assert entry.version == 3


async def test_migrate_v3_no_op(hass: HomeAssistant) -> None:
    """A v3 entry is not touched by async_migrate_entry."""
    entry = config_entries.ConfigEntry(
        entry_id="v3_entry",
        domain=DOMAIN,
        title="Battery Controller",
        data={**_BASE_BATTERY, **_BASE_SENSORS},
        options={},
        source="user",
        version=3,
        minor_version=1,
        unique_id=DOMAIN,
        discovery_keys=set(),
        subentries_data=None,
    )
    hass.config_entries._entries[entry.entry_id] = entry
    result = await async_migrate_entry(hass, entry)
    assert result is True
    assert entry.version == 3
    assert len(entry.subentries) == 0

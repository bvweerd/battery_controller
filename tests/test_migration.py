"""Config entry migration tests.

These tests run the migration through the REAL Home Assistant setup path
(hass.config_entries.async_setup) instead of calling async_migrate_entry
directly: HA looks the handler up on the integration package (__init__.py),
so a direct call would keep passing even when the handler is unreachable in
production ("Migration handler not found").
"""

from __future__ import annotations

import math

import pytest
from homeassistant.config_entries import ConfigEntryState, ConfigSubentry
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.battery_controller.const import (
    BATTERY_SUBENTRY_TYPE,
    CONF_BATTERY_SOC_SENSOR,
    CONF_CAPACITY_KWH,
    CONF_CHARGE_EFFICIENCY_CURVE,
    CONF_DISCHARGE_EFFICIENCY_CURVE,
    CONF_MAX_CHARGE_POWER_KW,
    CONF_MAX_DISCHARGE_POWER_KW,
    CONF_PRICE_SENSOR,
    CONF_ROUND_TRIP_EFFICIENCY,
    DOMAIN,
)
from custom_components.battery_controller.efficiency_curve import (
    parse_efficiency_curve,
)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading the custom integration in all tests."""
    yield


def _make_entry(
    hass: HomeAssistant, *, version: int, data: dict, subentries=()
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Battery Controller",
        data=data,
        version=version,
        minor_version=1,
        unique_id=DOMAIN,
    )
    entry.add_to_hass(hass)
    for sub in subentries:
        hass.config_entries.async_add_subentry(entry, sub)
    return entry


def _battery_subentry(data: dict) -> ConfigSubentry:
    return ConfigSubentry(
        subentry_type=BATTERY_SUBENTRY_TYPE,
        title="test battery",
        data=data,
        unique_id=None,
    )


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_v4_entry_migrates_through_ha_setup(hass: HomeAssistant) -> None:
    """A v4 entry must reach version 5 via the real HA setup path."""
    entry = _make_entry(
        hass,
        version=4,
        data={CONF_PRICE_SENSOR: "sensor.price"},
        subentries=[
            _battery_subentry(
                {
                    CONF_CAPACITY_KWH: 10.0,
                    CONF_MAX_CHARGE_POWER_KW: 5.0,
                    CONF_MAX_DISCHARGE_POWER_KW: 5.0,
                    CONF_ROUND_TRIP_EFFICIENCY: 0.81,
                    CONF_BATTERY_SOC_SENSOR: "sensor.soc",
                }
            )
        ],
    )

    await _setup(hass, entry)

    assert entry.state is not ConfigEntryState.MIGRATION_ERROR
    assert entry.version == 5
    sub = next(iter(entry.subentries.values()))
    assert CONF_ROUND_TRIP_EFFICIENCY not in sub.data
    # sqrt(0.81) = 0.9 per direction
    assert sub.data[CONF_CHARGE_EFFICIENCY_CURVE] == "0.900000"
    assert sub.data[CONF_DISCHARGE_EFFICIENCY_CURVE] == "0.900000"


@pytest.mark.parametrize("rte", [0.81, 0.90, 1.0])
async def test_v4_migration_preserves_round_trip_efficiency(
    hass: HomeAssistant, rte: float
) -> None:
    """charge_eff × discharge_eff of the migrated curves must equal the old RTE."""
    entry = _make_entry(
        hass,
        version=4,
        data={CONF_PRICE_SENSOR: "sensor.price"},
        subentries=[
            _battery_subentry(
                {
                    CONF_CAPACITY_KWH: 10.0,
                    CONF_MAX_CHARGE_POWER_KW: 5.0,
                    CONF_MAX_DISCHARGE_POWER_KW: 5.0,
                    CONF_ROUND_TRIP_EFFICIENCY: rte,
                    CONF_BATTERY_SOC_SENSOR: "sensor.soc",
                }
            )
        ],
    )

    await _setup(hass, entry)

    assert entry.version == 5
    sub = next(iter(entry.subentries.values()))
    charge = parse_efficiency_curve(sub.data[CONF_CHARGE_EFFICIENCY_CURVE], 5.0)
    discharge = parse_efficiency_curve(sub.data[CONF_DISCHARGE_EFFICIENCY_CURVE], 5.0)
    assert charge[0][1] * discharge[0][1] == pytest.approx(rte, abs=1e-3)
    assert charge[0][1] == pytest.approx(math.sqrt(rte), abs=1e-6)


async def test_v4_entry_with_curve_keys_is_left_alone(hass: HomeAssistant) -> None:
    """Subentries that already carry curve keys must not be rewritten."""
    entry = _make_entry(
        hass,
        version=4,
        data={CONF_PRICE_SENSOR: "sensor.price"},
        subentries=[
            _battery_subentry(
                {
                    CONF_CAPACITY_KWH: 10.0,
                    CONF_CHARGE_EFFICIENCY_CURVE: "0:0.95, 5:0.90",
                    CONF_DISCHARGE_EFFICIENCY_CURVE: "0.93",
                    CONF_BATTERY_SOC_SENSOR: "sensor.soc",
                }
            )
        ],
    )

    await _setup(hass, entry)

    assert entry.version == 5
    sub = next(iter(entry.subentries.values()))
    assert sub.data[CONF_CHARGE_EFFICIENCY_CURVE] == "0:0.95, 5:0.90"
    assert sub.data[CONF_DISCHARGE_EFFICIENCY_CURVE] == "0.93"


async def test_v3_entry_battery_moves_to_subentry(hass: HomeAssistant) -> None:
    """v3 entries kept battery specs in main data; they must become a subentry."""
    entry = _make_entry(
        hass,
        version=3,
        data={
            CONF_PRICE_SENSOR: "sensor.price",
            CONF_CAPACITY_KWH: 8.0,
            CONF_MAX_CHARGE_POWER_KW: 4.0,
            CONF_MAX_DISCHARGE_POWER_KW: 4.0,
            CONF_ROUND_TRIP_EFFICIENCY: 0.9,
            CONF_BATTERY_SOC_SENSOR: "sensor.soc",
        },
    )

    await _setup(hass, entry)

    assert entry.state is not ConfigEntryState.MIGRATION_ERROR
    assert entry.version == 5
    # Battery keys removed from main data
    assert CONF_CAPACITY_KWH not in entry.data
    assert CONF_ROUND_TRIP_EFFICIENCY not in entry.data
    assert entry.data[CONF_PRICE_SENSOR] == "sensor.price"
    # ... and moved into a battery subentry with curves derived from the RTE
    batteries = [
        s for s in entry.subentries.values() if s.subentry_type == BATTERY_SUBENTRY_TYPE
    ]
    assert len(batteries) == 1
    sub = batteries[0]
    assert sub.data[CONF_CAPACITY_KWH] == 8.0
    assert sub.data[CONF_BATTERY_SOC_SENSOR] == "sensor.soc"
    assert CONF_ROUND_TRIP_EFFICIENCY not in sub.data
    assert sub.data[CONF_CHARGE_EFFICIENCY_CURVE] == "0.948683"


async def test_migration_is_idempotent(hass: HomeAssistant) -> None:
    """Running setup twice (e.g. after a reload) must not change data again."""
    entry = _make_entry(
        hass,
        version=4,
        data={CONF_PRICE_SENSOR: "sensor.price"},
        subentries=[
            _battery_subentry(
                {
                    CONF_CAPACITY_KWH: 10.0,
                    CONF_ROUND_TRIP_EFFICIENCY: 0.9,
                    CONF_BATTERY_SOC_SENSOR: "sensor.soc",
                }
            )
        ],
    )

    await _setup(hass, entry)
    first_data = dict(next(iter(entry.subentries.values())).data)
    assert entry.version == 5

    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    await _setup(hass, entry)

    assert entry.version == 5
    assert dict(next(iter(entry.subentries.values())).data) == first_data

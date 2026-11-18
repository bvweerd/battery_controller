"""Entity names, end to end through Home Assistant's translation lookup.

Every entity is named through its translation key. A hard-coded _attr_name
takes precedence over the translation in Home Assistant, which is how the
Dutch names in nl.json used to be ignored entirely, and per-subentry sensors
repeated the device name ("Marstek Battery Setpoint Marstek").
"""

from __future__ import annotations

import pytest
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.battery_controller import binary_sensor, number, select
from custom_components.battery_controller import sensor, switch
from custom_components.battery_controller.const import (
    BATTERY_SUBENTRY_TYPE,
    CONF_BATTERY_SOC_SENSOR,
    CONF_CAPACITY_KWH,
    CONF_MAX_CHARGE_POWER_KW,
    CONF_MAX_DISCHARGE_POWER_KW,
    CONF_PRICE_SENSOR,
    DOMAIN,
)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable loading the custom integration in all tests."""
    yield


async def _setup(hass: HomeAssistant) -> tuple[MockConfigEntry, str]:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Battery Controller",
        data={CONF_PRICE_SENSOR: "sensor.price"},
        version=6,
        unique_id=DOMAIN,
    )
    entry.add_to_hass(hass)
    hass.config_entries.async_add_subentry(
        entry,
        ConfigSubentry(
            subentry_type=BATTERY_SUBENTRY_TYPE,
            title="Marstek",
            data={
                CONF_CAPACITY_KWH: 5.0,
                CONF_MAX_CHARGE_POWER_KW: 2.5,
                CONF_MAX_DISCHARGE_POWER_KW: 2.5,
                CONF_BATTERY_SOC_SENSOR: "sensor.soc",
            },
            unique_id=None,
        ),
    )
    hass.states.async_set("sensor.price", "0.25")
    hass.states.async_set("sensor.soc", "50", {"unit_of_measurement": "%"})
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    sid = next(iter(entry.subentries))
    return entry, sid


def _friendly_name(hass: HomeAssistant, platform: str, unique_id: str) -> str:
    entity_id = er.async_get(hass).async_get_entity_id(platform, DOMAIN, unique_id)
    assert entity_id is not None, unique_id
    state = hass.states.get(entity_id)
    assert state is not None, entity_id
    return str(state.attributes["friendly_name"])


async def test_english_names_and_no_repeated_device_name(hass: HomeAssistant):
    entry, sid = await _setup(hass)
    eid = entry.entry_id

    assert _friendly_name(hass, "sensor", f"{eid}_soc") == (
        "Battery Controller Total State of Charge"
    )
    assert _friendly_name(hass, "sensor", f"{eid}_setpoint_{sid}") == (
        "Marstek Battery Setpoint"
    )
    assert _friendly_name(hass, "number", f"{eid}_degradation_cost") == (
        "Battery Controller Degradation Cost per Cycle"
    )


async def test_dutch_translations_are_used(hass: HomeAssistant):
    hass.config.language = "nl"
    entry, sid = await _setup(hass)
    eid = entry.entry_id

    assert _friendly_name(hass, "sensor", f"{eid}_soc") == (
        "Battery Controller Totale Laadtoestand"
    )
    assert _friendly_name(hass, "sensor", f"{eid}_setpoint_{sid}") == (
        "Marstek Batterij Setpoint"
    )
    assert _friendly_name(hass, "switch", f"{eid}_optimization_enabled") == (
        "Battery Controller Optimalisatie Actief"
    )


@pytest.mark.parametrize("module", [sensor, binary_sensor, number, select, switch])
def test_no_entity_hard_codes_its_name(module) -> None:
    """A hard-coded name would override the translation again."""
    import inspect

    assert "_attr_name" not in inspect.getsource(module)

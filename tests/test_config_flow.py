"""Tests for the Battery Controller config flow."""

from __future__ import annotations


import pytest
from homeassistant import config_entries, setup
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.battery_controller.const import DOMAIN


@pytest.fixture(autouse=True)
async def setup_ha(hass: HomeAssistant) -> None:
    """Set up Home Assistant for testing."""
    await setup.async_setup_component(hass, "persistent_notification", {})


async def test_form_user_success(hass: HomeAssistant) -> None:
    """Test successful initial configuration."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input={
            "battery": {
                "capacity_kwh": 10.0,
                "max_charge_power_kw": 5.0,
                "max_discharge_power_kw": 5.0,
                "round_trip_efficiency": 0.9,
            },
            "sensors": {
                "price_sensor": "sensor.nordpool_kwh_se3_eur",
                "battery_soc_sensor": "sensor.battery_soc",
            },
        },
    )
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "pv_menu"


async def test_form_user_required_fields(hass: HomeAssistant) -> None:
    """Test user form with missing required fields raises schema validation errors."""
    from homeassistant.data_entry_flow import InvalidData

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    # Missing price_sensor should raise InvalidData from schema validation
    with pytest.raises(InvalidData):
        await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                "battery": {
                    "capacity_kwh": 10.0,
                    "max_charge_power_kw": 5.0,
                    "max_discharge_power_kw": 5.0,
                    "round_trip_efficiency": 0.9,
                },
                "sensors": {
                    # "price_sensor": "sensor.nordpool_kwh_se3_eur", # Missing
                    "battery_soc_sensor": "sensor.battery_soc",
                },
            },
        )


async def test_form_user_missing_battery_soc(hass: HomeAssistant) -> None:
    """Test user form with missing battery_soc_sensor raises schema validation error."""
    from homeassistant.data_entry_flow import InvalidData

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    # Missing battery_soc_sensor should raise InvalidData from schema validation
    with pytest.raises(InvalidData):
        await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={
                "battery": {
                    "capacity_kwh": 10.0,
                    "max_charge_power_kw": 5.0,
                    "max_discharge_power_kw": 5.0,
                    "round_trip_efficiency": 0.9,
                },
                "sensors": {
                    "price_sensor": "sensor.nordpool_kwh_se3_eur",
                    # "battery_soc_sensor": "sensor.battery_soc", # Missing
                },
            },
        )


async def test_options_flow_success(hass: HomeAssistant, config_entry) -> None:
    """Test successful options flow."""
    # Setup initial config entry
    config_entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    # Submit the initial form
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            "battery": {
                "capacity_kwh": 12.0,  # Changed value
                "max_charge_power_kw": 5.0,
                "max_discharge_power_kw": 5.0,
                "round_trip_efficiency": 0.9,
            },
            "sensors": {
                "price_sensor": "sensor.nordpool_kwh_se3_eur_new",  # Changed value
                "battery_soc_sensor": "sensor.battery_soc",
            },
        },
    )
    # Should go to PV menu
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "pv_menu"

    # Choose to finish setup without adding PV arrays
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={"next_step_id": "finish_setup"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert config_entry.options["battery_soc_sensor"] == "sensor.battery_soc"
    assert config_entry.options["capacity_kwh"] == 12.0
    assert config_entry.options["price_sensor"] == "sensor.nordpool_kwh_se3_eur_new"


# Fixture for a dummy config entry
@pytest.fixture
def config_entry(hass):
    """Create a dummy config entry."""
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    return MockConfigEntry(
        entry_id="test_entry",
        domain=DOMAIN,
        title="Test Battery Controller",
        data={
            "capacity_kwh": 10.0,
            "max_charge_power_kw": 5.0,
            "max_discharge_power_kw": 5.0,
            "round_trip_efficiency": 0.9,
            "price_sensor": "sensor.nordpool_kwh_se3_eur",
            "battery_soc_sensor": "sensor.battery_soc",
        },
        options={},
        source="user",
        version=1,
    )

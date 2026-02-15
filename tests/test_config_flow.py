"""Tests for the Battery Controller config flow."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant import config_entries, setup
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData

from custom_components.battery_controller.const import DOMAIN, CONF_PV_EXTRA_ARRAYS


@pytest.fixture(autouse=True)
async def setup_ha(hass: HomeAssistant) -> None:
    """Set up Home Assistant for testing."""
    await setup.async_setup_component(hass, "persistent_notification", {})


@pytest.fixture
def mock_config() -> dict:
    """Return a mock config for the integration."""
    return {
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
    }


async def test_form_user_success(hass: HomeAssistant, mock_config: dict) -> None:
    """Test successful initial configuration."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input=mock_config,
    )
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "pv_menu"


async def test_form_user_required_fields(hass: HomeAssistant) -> None:
    """Test user form with missing required fields."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )

    # Missing price_sensor
    with pytest.raises(InvalidData) as exc_info:
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
                    "battery_soc_sensor": "sensor.battery_soc",
                },
            },
        )
    assert "data['sensors']['price_sensor']" in str(exc_info.value)

    # Missing battery_soc_sensor
    with pytest.raises(InvalidData) as exc_info:
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
                },
            },
        )
    assert "data['sensors']['battery_soc_sensor']" in str(exc_info.value)


async def test_pv_array_add_only_flow(hass: HomeAssistant, mock_config: dict) -> None:
    """Test only adding a single PV array from the menu."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=mock_config
    )
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "pv_menu"
    print(f"Menu Result: {result}")

    # Select "add_pv_array" from the menu
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input={"next_step_id": "add_pv_array"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "add_pv_array"

    pv_array_input = {
        "peak_power_kwp": 4.0,
        "orientation": 90.0,
        "tilt": 30.0,
        "dc_coupled": False,
    }
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], user_input=pv_array_input
    )
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "pv_menu"
    assert result["description_placeholders"]["pv_count"] == "1"

    # Finish setup — mock async_setup_entry to avoid real HTTP calls in flow tests
    with patch(
        "custom_components.battery_controller.async_setup_entry",
        return_value=True,
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={"next_step_id": "finish_setup"}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Battery Controller"
    assert len(result["data"][CONF_PV_EXTRA_ARRAYS]) == 1
    assert result["data"][CONF_PV_EXTRA_ARRAYS][0]["peak_power_kwp"] == 4.0


async def test_options_flow_success(hass: HomeAssistant, mock_config: dict) -> None:
    """Test successful options flow."""
    # Create initial data with PV arrays
    initial_data = {}
    for section_key, section_data in mock_config.items():
        initial_data.update(section_data)
    initial_data[CONF_PV_EXTRA_ARRAYS] = [
        {"peak_power_kwp": 3.0, "orientation": 100.0, "tilt": 20.0, "dc_coupled": False}
    ]

    # Create config entry
    config_entry = config_entries.ConfigEntry(
        entry_id="test_entry",
        domain=DOMAIN,
        title="Test Battery Controller",
        data=initial_data,
        options={},
        source="user",
        version=2,
        minor_version=1,
        unique_id=None,
        discovery_keys=set(),
        subentries_data=None,
    )

    # Setup initial config entry
    hass.config_entries._entries[config_entry.entry_id] = config_entry

    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    # Change some values and go to PV menu
    updated_config = mock_config.copy()
    updated_config["battery"]["capacity_kwh"] = 12.0
    updated_config["sensors"]["price_sensor"] = "sensor.new_price_sensor"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input=updated_config,
    )
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "pv_menu"
    assert result["description_placeholders"]["pv_count"] == "1"  # Retains existing

    # Add another PV array in options flow
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={"next_step_id": "add_pv_array"}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "add_pv_array"

    pv_array_input_new = {
        "peak_power_kwp": 5.0,
        "orientation": 150.0,
        "tilt": 25.0,
        "dc_coupled": False,
    }
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input=pv_array_input_new
    )
    assert result["type"] is FlowResultType.MENU
    assert result["step_id"] == "pv_menu"
    assert result["description_placeholders"]["pv_count"] == "2"

    # Finish options flow
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={"next_step_id": "finish_setup"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert config_entry.options["capacity_kwh"] == 12.0
    assert config_entry.options["price_sensor"] == "sensor.new_price_sensor"
    assert len(config_entry.options[CONF_PV_EXTRA_ARRAYS]) == 2
    assert config_entry.options[CONF_PV_EXTRA_ARRAYS][1]["peak_power_kwp"] == 5.0

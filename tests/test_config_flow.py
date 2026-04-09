"""Tests for the Battery Controller config flow, PV subentry flow, and battery subentry flow."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from homeassistant import config_entries, setup
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData

from custom_components.battery_controller.const import (
    BATTERY_SUBENTRY_TYPE,
    DOMAIN,
    PV_SUBENTRY_TYPE,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
async def setup_ha(hass: HomeAssistant) -> None:
    """Set up Home Assistant for testing."""
    await setup.async_setup_component(hass, "persistent_notification", {})


@pytest.fixture(autouse=True)
def mock_api_connection():
    """Mock the open-meteo connection test so config flow tests don't hit the network."""
    with patch(
        "custom_components.battery_controller.config_flow._test_api_connection",
        return_value=None,
    ):
        yield


@pytest.fixture
def mock_config() -> dict:
    """Minimal valid config flow input (v4 schema — no battery section)."""
    return {
        "sensors": {
            "price_sensor": "sensor.nordpool_kwh_se3_eur",
        },
    }


@pytest.fixture
def pv_subentry_data() -> dict:
    """Standard AC PV subentry input."""
    return {
        "peak_power_kwp": 4.0,
        "orientation": 180.0,
        "tilt": 35.0,
        "efficiency_factor": 0.85,
        "dc_coupled": False,
    }


@pytest.fixture
def battery_subentry_data() -> dict:
    """Standard battery subentry input."""
    return {
        "capacity_kwh": 10.0,
        "max_charge_power_kw": 5.0,
        "max_discharge_power_kw": 5.0,
        "round_trip_efficiency": 0.9,
        "min_soc_percent": 10.0,
        "max_soc_percent": 90.0,
        "battery_soc_sensor": "sensor.battery_soc",
        "pv_dc_efficiency": 0.97,
    }


@pytest.fixture
def v4_config_entry(hass: HomeAssistant) -> config_entries.ConfigEntry:
    """A v4 config entry (no battery specs in main data — those are subentries)."""
    entry = config_entries.ConfigEntry(
        entry_id="test_entry_v4",
        domain=DOMAIN,
        title="Battery Controller",
        data={
            "price_sensor": "sensor.price",
        },
        options={},
        source="user",
        version=4,
        minor_version=1,
        unique_id=DOMAIN,
        discovery_keys=set(),
        subentries_data=None,
    )
    hass.config_entries._entries[entry.entry_id] = entry
    return entry


@pytest.fixture
def v3_config_entry(hass: HomeAssistant) -> config_entries.ConfigEntry:
    """A v3 config entry with battery specs in main data (pre-migration)."""
    entry = config_entries.ConfigEntry(
        entry_id="test_entry_v3",
        domain=DOMAIN,
        title="Battery Controller",
        data={
            "capacity_kwh": 10.0,
            "max_charge_power_kw": 5.0,
            "max_discharge_power_kw": 5.0,
            "round_trip_efficiency": 0.9,
            "pv_dc_efficiency": 0.97,
            "price_sensor": "sensor.price",
            "battery_soc_sensor": "sensor.soc",
        },
        options={},
        source="user",
        version=3,
        minor_version=1,
        unique_id=DOMAIN,
        discovery_keys=set(),
        subentries_data=None,
    )
    hass.config_entries._entries[entry.entry_id] = entry
    return entry


@pytest.fixture
def v4_config_entry_with_battery(
    hass: HomeAssistant,
    v4_config_entry: config_entries.ConfigEntry,
    battery_subentry_data: dict,
) -> config_entries.ConfigEntry:
    """A v4 config entry with one battery subentry."""
    from homeassistant.config_entries import ConfigSubentry

    sub = ConfigSubentry(
        subentry_type=BATTERY_SUBENTRY_TYPE,
        title="10.0 kWh",
        data=battery_subentry_data,
        unique_id=None,
    )
    hass.config_entries.async_add_subentry(v4_config_entry, sub)
    return v4_config_entry


@pytest.fixture
def v3_config_entry_with_pv_subentries(
    hass: HomeAssistant,
    v3_config_entry: config_entries.ConfigEntry,
    pv_subentry_data: dict,
) -> config_entries.ConfigEntry:
    """A v3 config entry pre-loaded with two PV subentries."""
    from homeassistant.config_entries import ConfigSubentry

    for i, (orientation, dc) in enumerate([(180.0, False), (90.0, True)], 1):
        data = {**pv_subentry_data, "orientation": orientation, "dc_coupled": dc}
        sub = ConfigSubentry(
            subentry_type=PV_SUBENTRY_TYPE,
            title=f"PV Array {i}",
            data=data,
            unique_id=None,
        )
        hass.config_entries.async_add_subentry(v3_config_entry, sub)
    return v3_config_entry


# keep the old name as alias for PV-specific tests
@pytest.fixture
def v3_config_entry_with_subentries(
    v3_config_entry_with_pv_subentries: config_entries.ConfigEntry,
) -> config_entries.ConfigEntry:
    return v3_config_entry_with_pv_subentries


# ---------------------------------------------------------------------------
# Config flow — initial setup (v4 schema)
# ---------------------------------------------------------------------------


async def test_form_user_shows_form(hass: HomeAssistant) -> None:
    """Config flow starts with the user form."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"


async def test_form_user_success_creates_entry(
    hass: HomeAssistant, mock_config: dict
) -> None:
    """Successful config flow creates an entry with only price sensor in data."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with (
        patch(
            "custom_components.battery_controller.config_flow._test_api_connection",
            return_value=None,
        ),
        patch(
            "custom_components.battery_controller.async_setup_entry", return_value=True
        ),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=mock_config
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Battery Controller"
    # Battery specs are now in subentries, not in main data
    assert "capacity_kwh" not in result["data"]
    assert "battery_soc_sensor" not in result["data"]
    assert result["data"]["price_sensor"] == "sensor.nordpool_kwh_se3_eur"


async def test_form_user_cannot_connect(hass: HomeAssistant, mock_config: dict) -> None:
    """Config flow shows error when open-meteo is unreachable."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with patch(
        "custom_components.battery_controller.config_flow._test_api_connection",
        return_value="cannot_connect",
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=mock_config
        )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"]["base"] == "cannot_connect"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 0


async def test_form_user_missing_price_sensor_returns_form_error(
    hass: HomeAssistant,
) -> None:
    """Missing price sensor is rejected by schema validation before flow handling."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with pytest.raises(InvalidData) as exc_info:
        await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"sensors": {}},
        )
    assert "price_sensor" in str(exc_info.value)
    assert len(hass.config_entries.async_entries(DOMAIN)) == 0


async def test_form_user_missing_price_sensor(hass: HomeAssistant) -> None:
    """Missing price_sensor raises InvalidData."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with pytest.raises(InvalidData) as exc_info:
        await hass.config_entries.flow.async_configure(
            result["flow_id"],
            user_input={"sensors": {}},
        )
    assert "price_sensor" in str(exc_info.value)


async def test_already_configured_aborts(
    hass: HomeAssistant, mock_config: dict
) -> None:
    """Second setup attempt aborts with already_configured."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with patch(
        "custom_components.battery_controller.async_setup_entry", return_value=True
    ):
        await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=mock_config
        )

    result2 = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result2["type"] is FlowResultType.ABORT
    assert result2["reason"] == "already_configured"


def test_extract_main_data_prefers_sections_and_defaults() -> None:
    """Section values take precedence and defaults fill missing optional fields."""
    from custom_components.battery_controller.config_flow import _extract_main_data

    data = _extract_main_data(
        {
            "price_sensor": "sensor.flat_price",
            "fixed_feed_in_price": "0.11",
            "sensors": {"price_sensor": "sensor.section_price"},
            "advanced": {"zero_grid_enabled": True},
        }
    )

    assert data["price_sensor"] == "sensor.section_price"
    assert data["feed_in_price_sensor"] is None
    assert data["power_consumption_sensors"] == []
    assert data["power_production_sensors"] == []
    assert data["electricity_consumption_sensors"] == []
    assert data["electricity_production_sensors"] == []
    assert data["pv_production_sensors"] == []
    assert data["fixed_feed_in_price"] == 0.11
    assert data["zero_grid_enabled"] is True
    assert data["zero_grid_response_time_s"] == 10.0
    assert data["max_grid_power_kw"] == 0.0


def test_extract_main_data_supports_flat_layout_fallback() -> None:
    """Flat keys remain supported when section wrappers are absent."""
    from custom_components.battery_controller.config_flow import _extract_main_data

    data = _extract_main_data(
        {
            "price_sensor": "sensor.flat_price",
            "feed_in_price_sensor": "sensor.feed_in",
            "power_consumption_sensors": ["sensor.load_1"],
            "pv_production_sensors": ["sensor.pv_total"],
            "fixed_feed_in_price": "0.07",
            "zero_grid_enabled": True,
            "zero_grid_response_time_s": "12",
            "max_grid_power_kw": "8.5",
        }
    )

    assert data["price_sensor"] == "sensor.flat_price"
    assert data["feed_in_price_sensor"] == "sensor.feed_in"
    assert data["power_consumption_sensors"] == ["sensor.load_1"]
    assert data["pv_production_sensors"] == ["sensor.pv_total"]
    assert data["fixed_feed_in_price"] == 0.07
    assert data["zero_grid_enabled"] is True
    assert data["zero_grid_response_time_s"] == 12.0
    assert data["max_grid_power_kw"] == 8.5


# ---------------------------------------------------------------------------
# Battery subentry flow — add
# ---------------------------------------------------------------------------


async def _init_battery_subentry_flow(hass: HomeAssistant, entry_id: str) -> dict:
    """Helper: start a battery subentry add flow."""
    return await hass.config_entries.subentries.async_init(
        (entry_id, BATTERY_SUBENTRY_TYPE),
        context={"source": "user"},
    )


async def test_battery_subentry_add(
    hass: HomeAssistant,
    v4_config_entry: config_entries.ConfigEntry,
    battery_subentry_data: dict,
) -> None:
    """Adding a battery subentry creates a subentry with correct data."""
    result = await _init_battery_subentry_flow(hass, v4_config_entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input=battery_subentry_data
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert len(v4_config_entry.subentries) == 1
    sub = list(v4_config_entry.subentries.values())[0]
    assert sub.data["capacity_kwh"] == 10.0
    assert sub.data["min_soc_percent"] == 10.0
    assert sub.data["max_soc_percent"] == 90.0
    assert sub.data["battery_soc_sensor"] == "sensor.battery_soc"
    assert "10.0 kWh" in sub.title


async def test_battery_subentry_add_multiple(
    hass: HomeAssistant,
    v4_config_entry: config_entries.ConfigEntry,
    battery_subentry_data: dict,
) -> None:
    """Two batteries can be added independently."""
    for cap in [10.0, 5.0]:
        result = await _init_battery_subentry_flow(hass, v4_config_entry.entry_id)
        await hass.config_entries.subentries.async_configure(
            result["flow_id"],
            user_input={**battery_subentry_data, "capacity_kwh": cap},
        )

    assert len(v4_config_entry.subentries) == 2
    capacities = {s.data["capacity_kwh"] for s in v4_config_entry.subentries.values()}
    assert capacities == {10.0, 5.0}


async def test_battery_subentry_invalid_capacity_rejected(
    hass: HomeAssistant,
    v4_config_entry: config_entries.ConfigEntry,
    battery_subentry_data: dict,
) -> None:
    """Capacity of 0 kWh is rejected."""
    result = await _init_battery_subentry_flow(hass, v4_config_entry.entry_id)
    with pytest.raises(InvalidData):
        await hass.config_entries.subentries.async_configure(
            result["flow_id"],
            user_input={**battery_subentry_data, "capacity_kwh": 0.0},
        )
    assert len(v4_config_entry.subentries) == 0


async def test_battery_subentry_invalid_rte_rejected(
    hass: HomeAssistant,
    v4_config_entry: config_entries.ConfigEntry,
    battery_subentry_data: dict,
) -> None:
    """RTE > 1.0 is rejected."""
    result = await _init_battery_subentry_flow(hass, v4_config_entry.entry_id)
    with pytest.raises(InvalidData):
        await hass.config_entries.subentries.async_configure(
            result["flow_id"],
            user_input={**battery_subentry_data, "round_trip_efficiency": 1.5},
        )
    assert len(v4_config_entry.subentries) == 0


# ---------------------------------------------------------------------------
# Battery subentry flow — reconfigure (edit)
# ---------------------------------------------------------------------------


async def test_battery_subentry_reconfigure(
    hass: HomeAssistant,
    v4_config_entry_with_battery: config_entries.ConfigEntry,
) -> None:
    """Reconfiguring a battery subentry updates its data."""
    entry = v4_config_entry_with_battery
    subentry_id = list(entry.subentries.keys())[0]

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, BATTERY_SUBENTRY_TYPE),
        context={"source": "reconfigure", "subentry_id": subentry_id},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input={
            "capacity_kwh": 15.0,
            "max_charge_power_kw": 7.5,
            "max_discharge_power_kw": 7.5,
            "round_trip_efficiency": 0.92,
            "min_soc_percent": 15.0,
            "max_soc_percent": 85.0,
            "battery_soc_sensor": "sensor.battery_soc_new",
            "pv_dc_efficiency": 0.97,
        },
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.subentries[subentry_id].data["capacity_kwh"] == 15.0
    assert entry.subentries[subentry_id].data["min_soc_percent"] == 15.0
    assert "15.0 kWh" in entry.subentries[subentry_id].title


async def test_battery_subentry_reconfigure_invalid_input_keeps_existing_data(
    hass: HomeAssistant,
    v4_config_entry_with_battery: config_entries.ConfigEntry,
) -> None:
    """Schema-invalid battery reconfigure input should leave the subentry untouched."""
    entry = v4_config_entry_with_battery
    subentry_id = list(entry.subentries.keys())[0]
    original_data = dict(entry.subentries[subentry_id].data)
    original_title = entry.subentries[subentry_id].title

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, BATTERY_SUBENTRY_TYPE),
        context={"source": "reconfigure", "subentry_id": subentry_id},
    )
    with pytest.raises(InvalidData):
        await hass.config_entries.subentries.async_configure(
            result["flow_id"],
            user_input={
                **original_data,
                "max_soc_percent": 120.0,
            },
        )
    assert dict(entry.subentries[subentry_id].data) == original_data
    assert entry.subentries[subentry_id].title == original_title


# ---------------------------------------------------------------------------
# PV subentry flow — add
# ---------------------------------------------------------------------------


async def _init_subentry_add_flow(hass: HomeAssistant, entry_id: str) -> dict:
    """Helper: start a PV subentry add flow and return the first result."""
    return await hass.config_entries.subentries.async_init(
        (entry_id, PV_SUBENTRY_TYPE),
        context={"source": "user"},
    )


async def test_subentry_add_ac_array(
    hass: HomeAssistant,
    v4_config_entry: config_entries.ConfigEntry,
    pv_subentry_data: dict,
) -> None:
    """Adding an AC PV array creates a subentry with correct data."""
    result = await _init_subentry_add_flow(hass, v4_config_entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input=pv_subentry_data
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert len(v4_config_entry.subentries) == 1
    sub = list(v4_config_entry.subentries.values())[0]
    assert sub.data["peak_power_kwp"] == 4.0
    assert sub.data["dc_coupled"] is False
    assert "AC" in sub.title


async def test_subentry_add_dc_coupled_array(
    hass: HomeAssistant,
    v4_config_entry: config_entries.ConfigEntry,
) -> None:
    """Adding a DC-coupled PV array is stored with dc_coupled=True."""
    result = await _init_subentry_add_flow(hass, v4_config_entry.entry_id)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input={
            "peak_power_kwp": 3.0,
            "orientation": 180.0,
            "tilt": 35.0,
            "efficiency_factor": 0.97,
            "dc_coupled": True,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    sub = list(v4_config_entry.subentries.values())[0]
    assert sub.data["dc_coupled"] is True
    assert "DC" in sub.title


async def test_subentry_add_multiple_arrays(
    hass: HomeAssistant,
    v4_config_entry: config_entries.ConfigEntry,
    pv_subentry_data: dict,
) -> None:
    """Three arrays added sequentially → three independent subentries."""
    for orientation in [180.0, 90.0, 270.0]:
        result = await _init_subentry_add_flow(hass, v4_config_entry.entry_id)
        await hass.config_entries.subentries.async_configure(
            result["flow_id"],
            user_input={**pv_subentry_data, "orientation": orientation},
        )

    assert len(v4_config_entry.subentries) == 3
    orientations = {s.data["orientation"] for s in v4_config_entry.subentries.values()}
    assert orientations == {180.0, 90.0, 270.0}


async def test_subentry_add_zero_kwp_rejected(
    hass: HomeAssistant,
    v4_config_entry: config_entries.ConfigEntry,
) -> None:
    """peak_power_kwp=0.0 fails voluptuous validation."""
    result = await _init_subentry_add_flow(hass, v4_config_entry.entry_id)
    with pytest.raises(InvalidData):
        await hass.config_entries.subentries.async_configure(
            result["flow_id"],
            user_input={
                "peak_power_kwp": 0.0,
                "orientation": 180.0,
                "tilt": 35.0,
                "efficiency_factor": 0.85,
                "dc_coupled": False,
            },
        )
    assert len(v4_config_entry.subentries) == 0


async def test_subentry_add_invalid_orientation_rejected(
    hass: HomeAssistant,
    v4_config_entry: config_entries.ConfigEntry,
) -> None:
    """Orientation outside 0-360 range is rejected."""
    result = await _init_subentry_add_flow(hass, v4_config_entry.entry_id)
    with pytest.raises(InvalidData):
        await hass.config_entries.subentries.async_configure(
            result["flow_id"],
            user_input={
                "peak_power_kwp": 4.0,
                "orientation": 400.0,
                "tilt": 35.0,
                "efficiency_factor": 0.85,
                "dc_coupled": False,
            },
        )
    assert len(v4_config_entry.subentries) == 0


async def test_subentry_add_mixed_ac_dc(
    hass: HomeAssistant,
    v4_config_entry: config_entries.ConfigEntry,
    pv_subentry_data: dict,
) -> None:
    """Mix of AC and DC subentries are stored independently."""
    result = await _init_subentry_add_flow(hass, v4_config_entry.entry_id)
    await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input=pv_subentry_data
    )
    result = await _init_subentry_add_flow(hass, v4_config_entry.entry_id)
    await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input={**pv_subentry_data, "dc_coupled": True, "peak_power_kwp": 2.0},
    )

    assert len(v4_config_entry.subentries) == 2
    dc_count = sum(
        1 for s in v4_config_entry.subentries.values() if s.data.get("dc_coupled")
    )
    ac_count = sum(
        1 for s in v4_config_entry.subentries.values() if not s.data.get("dc_coupled")
    )
    assert ac_count == 1
    assert dc_count == 1


# ---------------------------------------------------------------------------
# PV subentry flow — reconfigure (edit)
# ---------------------------------------------------------------------------


async def test_subentry_reconfigure_updates_data(
    hass: HomeAssistant,
    v3_config_entry_with_subentries: config_entries.ConfigEntry,
) -> None:
    """Reconfiguring a PV subentry updates its data."""
    entry = v3_config_entry_with_subentries
    pv_ids = [
        sid
        for sid, s in entry.subentries.items()
        if s.subentry_type == PV_SUBENTRY_TYPE
    ]
    subentry_id = pv_ids[0]
    original_orientation = entry.subentries[subentry_id].data["orientation"]

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, PV_SUBENTRY_TYPE),
        context={"source": "reconfigure", "subentry_id": subentry_id},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"

    new_orientation = (original_orientation + 45.0) % 360.0
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input={
            "peak_power_kwp": 6.0,
            "orientation": new_orientation,
            "tilt": 30.0,
            "efficiency_factor": 0.80,
            "dc_coupled": False,
        },
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.subentries[subentry_id].data["peak_power_kwp"] == 6.0
    assert entry.subentries[subentry_id].data["orientation"] == new_orientation


async def test_subentry_reconfigure_invalid_input_keeps_existing_data(
    hass: HomeAssistant,
    v3_config_entry_with_subentries: config_entries.ConfigEntry,
) -> None:
    """Schema-invalid PV reconfigure input should leave the existing subentry untouched."""
    entry = v3_config_entry_with_subentries
    subentry_id = next(iter(entry.subentries.keys()))
    original_data = dict(entry.subentries[subentry_id].data)
    original_title = entry.subentries[subentry_id].title

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, PV_SUBENTRY_TYPE),
        context={"source": "reconfigure", "subentry_id": subentry_id},
    )
    with pytest.raises(InvalidData):
        await hass.config_entries.subentries.async_configure(
            result["flow_id"],
            user_input={
                **original_data,
                "tilt": 120.0,
            },
        )
    assert dict(entry.subentries[subentry_id].data) == original_data
    assert entry.subentries[subentry_id].title == original_title


async def test_subentry_reconfigure_leaves_others_unchanged(
    hass: HomeAssistant,
    v3_config_entry_with_subentries: config_entries.ConfigEntry,
) -> None:
    """Editing one subentry does not touch the other."""
    entry = v3_config_entry_with_subentries
    pv_ids = [
        sid
        for sid, s in entry.subentries.items()
        if s.subentry_type == PV_SUBENTRY_TYPE
    ]
    target_id, other_id = pv_ids[0], pv_ids[1]
    other_data_before = dict(entry.subentries[other_id].data)

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, PV_SUBENTRY_TYPE),
        context={"source": "reconfigure", "subentry_id": target_id},
    )
    await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input={
            "peak_power_kwp": 8.0,
            "orientation": 200.0,
            "tilt": 40.0,
            "efficiency_factor": 0.9,
            "dc_coupled": False,
        },
    )
    assert dict(entry.subentries[other_id].data) == other_data_before


# ---------------------------------------------------------------------------
# Subentry — delete
# ---------------------------------------------------------------------------


async def test_subentry_delete(
    hass: HomeAssistant,
    v3_config_entry_with_subentries: config_entries.ConfigEntry,
) -> None:
    """Deleting a subentry removes it; the other remains."""
    entry = v3_config_entry_with_subentries
    ids = list(entry.subentries.keys())
    assert len(ids) == 2

    hass.config_entries.async_remove_subentry(entry, ids[0])

    assert len(entry.subentries) == 1
    assert ids[0] not in entry.subentries
    assert ids[1] in entry.subentries


async def test_battery_subentry_delete(
    hass: HomeAssistant,
    v4_config_entry_with_battery: config_entries.ConfigEntry,
) -> None:
    """Deleting a battery subentry removes it."""
    entry = v4_config_entry_with_battery
    assert len(entry.subentries) == 1
    sub_id = list(entry.subentries.keys())[0]

    hass.config_entries.async_remove_subentry(entry, sub_id)
    assert len(entry.subentries) == 0


# ---------------------------------------------------------------------------
# _validate_battery_subentry — uncovered branches
# ---------------------------------------------------------------------------


def test_validate_battery_subentry_with_name_stores_name():
    """Name is non-empty → result[CONF_NAME] is set (line 164)."""
    from custom_components.battery_controller.config_flow import (
        _validate_battery_subentry,
    )

    data = {
        "name": "My Battery",
        "capacity_kwh": 10.0,
        "max_charge_power_kw": 5.0,
        "max_discharge_power_kw": 5.0,
        "round_trip_efficiency": 0.9,
        "min_soc_percent": 10.0,
        "max_soc_percent": 90.0,
        "battery_soc_sensor": "sensor.soc",
        "pv_dc_efficiency": 0.97,
    }
    result = _validate_battery_subentry(data)
    assert result["name"] == "My Battery"


def test_validate_battery_subentry_with_power_sensor():
    """battery_power_sensor present → stored in result (line 180)."""
    from custom_components.battery_controller.config_flow import (
        _validate_battery_subentry,
    )

    data = {
        "capacity_kwh": 10.0,
        "max_charge_power_kw": 5.0,
        "max_discharge_power_kw": 5.0,
        "round_trip_efficiency": 0.9,
        "min_soc_percent": 10.0,
        "max_soc_percent": 90.0,
        "battery_soc_sensor": "sensor.soc",
        "pv_dc_efficiency": 0.97,
        "battery_power_sensor": "sensor.power",
    }
    result = _validate_battery_subentry(data)
    assert result["battery_power_sensor"] == "sensor.power"


def test_validate_battery_subentry_with_soc_derating_keys():
    """SoC derating keys present → stored in result (line 189)."""
    from custom_components.battery_controller.config_flow import (
        _validate_battery_subentry,
    )
    from custom_components.battery_controller.const import (
        CONF_HIGH_SOC_CHARGE_THRESHOLD_PCT,
        CONF_HIGH_SOC_MAX_CHARGE_KW,
    )

    data = {
        "capacity_kwh": 10.0,
        "max_charge_power_kw": 5.0,
        "max_discharge_power_kw": 5.0,
        "round_trip_efficiency": 0.9,
        "min_soc_percent": 10.0,
        "max_soc_percent": 90.0,
        "battery_soc_sensor": "sensor.soc",
        "pv_dc_efficiency": 0.97,
        CONF_HIGH_SOC_CHARGE_THRESHOLD_PCT: 80.0,
        CONF_HIGH_SOC_MAX_CHARGE_KW: 2.5,
    }
    result = _validate_battery_subentry(data)
    assert result[CONF_HIGH_SOC_CHARGE_THRESHOLD_PCT] == 80.0
    assert result[CONF_HIGH_SOC_MAX_CHARGE_KW] == 2.5


def test_validate_battery_subentry_whitespace_name_is_dropped():
    """Whitespace-only names should not be stored in normalized battery data."""
    from custom_components.battery_controller.config_flow import (
        _validate_battery_subentry,
    )

    data = {
        "name": "   ",
        "capacity_kwh": 10.0,
        "max_charge_power_kw": 5.0,
        "max_discharge_power_kw": 5.0,
        "round_trip_efficiency": 0.9,
        "min_soc_percent": 10.0,
        "max_soc_percent": 90.0,
        "battery_soc_sensor": "sensor.soc",
        "pv_dc_efficiency": 0.97,
    }
    result = _validate_battery_subentry(data)
    assert "name" not in result


def test_validate_battery_subentry_omits_optional_keys_when_absent():
    """Optional sensors and derating keys are omitted when not provided."""
    from custom_components.battery_controller.config_flow import (
        _validate_battery_subentry,
    )

    data = {
        "capacity_kwh": 10.0,
        "max_charge_power_kw": 5.0,
        "max_discharge_power_kw": 5.0,
        "round_trip_efficiency": 0.9,
        "min_soc_percent": 10.0,
        "max_soc_percent": 90.0,
        "battery_soc_sensor": "sensor.soc",
        "pv_dc_efficiency": 0.97,
    }
    result = _validate_battery_subentry(data)
    assert "battery_power_sensor" not in result
    assert "high_soc_charge_threshold_pct" not in result
    assert "high_soc_max_charge_kw" not in result
    assert "low_soc_discharge_threshold_pct" not in result
    assert "low_soc_max_discharge_kw" not in result


def test_battery_subentry_title_with_name():
    """_battery_subentry_title returns name when non-empty (line 196)."""
    from custom_components.battery_controller.config_flow import _battery_subentry_title

    assert (
        _battery_subentry_title({"name": "Main Battery", "capacity_kwh": 10.0})
        == "Main Battery"
    )


def test_pv_subentry_title_with_name():
    """_pv_subentry_title returns name when non-empty (line 506)."""
    from custom_components.battery_controller.config_flow import _pv_subentry_title

    assert (
        _pv_subentry_title(
            {"name": "Roof PV", "peak_power_kwp": 4.0, "dc_coupled": False}
        )
        == "Roof PV"
    )


def test_validate_pv_subentry_with_name():
    """_validate_pv_subentry stores name when non-empty (line 490)."""
    from custom_components.battery_controller.config_flow import _validate_pv_subentry

    data = {
        "name": "South Array",
        "peak_power_kwp": 4.0,
        "orientation": 180.0,
        "tilt": 35.0,
        "efficiency_factor": 0.85,
        "dc_coupled": False,
    }
    result = _validate_pv_subentry(data)
    assert result["name"] == "South Array"


def test_validate_pv_subentry_whitespace_name_is_dropped():
    """Whitespace-only names should not be stored in normalized PV data."""
    from custom_components.battery_controller.config_flow import _validate_pv_subentry

    data = {
        "name": "   ",
        "peak_power_kwp": 4.0,
        "orientation": 180.0,
        "tilt": 35.0,
        "efficiency_factor": 0.85,
        "dc_coupled": False,
    }
    result = _validate_pv_subentry(data)
    assert "name" not in result


# ---------------------------------------------------------------------------
# Options flow — missing_required error path (line 569)
# ---------------------------------------------------------------------------


async def test_options_flow_missing_price_sensor_shows_error(
    hass: HomeAssistant,
    v4_config_entry: config_entries.ConfigEntry,
) -> None:
    """Submitting options without price_sensor fails schema validation."""
    result = await hass.config_entries.options.async_init(v4_config_entry.entry_id)
    with pytest.raises(InvalidData) as exc_info:
        await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={"sensors": {}},
        )
    assert "price_sensor" in str(exc_info.value)
    assert v4_config_entry.options == {}


# ---------------------------------------------------------------------------
# Error paths in subentry flows (lines 391-392, 416-417, 443-444, 468-469)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_battery_subentry_user_vol_invalid_sets_error():
    """vol.Invalid in _validate_battery_subentry sets errors['base'] (line 391-392)."""
    import voluptuous as vol

    from custom_components.battery_controller.config_flow import (
        BatteryControllerBatterySubentryFlow,
    )

    flow = BatteryControllerBatterySubentryFlow()
    flow.async_show_form = MagicMock(return_value={"type": "form", "errors": {}})

    with patch(
        "custom_components.battery_controller.config_flow._validate_battery_subentry",
        side_effect=vol.Invalid("bad input"),
    ):
        await flow.async_step_user(user_input={"capacity_kwh": 10.0})

    call_kwargs = flow.async_show_form.call_args
    assert call_kwargs.kwargs.get("errors", {}).get("base") == "invalid_battery_input"


@pytest.mark.asyncio
async def test_battery_subentry_reconfigure_vol_invalid_sets_error():
    """vol.Invalid in battery reconfigure sets errors['base'] (line 416-417)."""
    import voluptuous as vol

    from custom_components.battery_controller.config_flow import (
        BatteryControllerBatterySubentryFlow,
    )

    flow = BatteryControllerBatterySubentryFlow()
    flow.async_show_form = MagicMock(return_value={"type": "form", "errors": {}})
    flow._get_entry = MagicMock()
    flow._get_reconfigure_subentry = MagicMock()
    flow._get_reconfigure_subentry.return_value.data = {
        "capacity_kwh": 10.0,
        "max_charge_power_kw": 5.0,
    }

    with patch(
        "custom_components.battery_controller.config_flow._validate_battery_subentry",
        side_effect=vol.Invalid("bad input"),
    ):
        await flow.async_step_reconfigure(user_input={"capacity_kwh": 10.0})

    call_kwargs = flow.async_show_form.call_args
    assert call_kwargs.kwargs.get("errors", {}).get("base") == "invalid_battery_input"


@pytest.mark.asyncio
async def test_pv_subentry_user_vol_invalid_sets_error():
    """vol.Invalid in _validate_pv_subentry sets errors['base'] (line 443-444)."""
    import voluptuous as vol

    from custom_components.battery_controller.config_flow import (
        BatteryControllerPVSubentryFlow,
    )

    flow = BatteryControllerPVSubentryFlow()
    flow.async_show_form = MagicMock(return_value={"type": "form", "errors": {}})

    with patch(
        "custom_components.battery_controller.config_flow._validate_pv_subentry",
        side_effect=vol.Invalid("bad pv"),
    ):
        await flow.async_step_user(user_input={"peak_power_kwp": 4.0})

    call_kwargs = flow.async_show_form.call_args
    assert call_kwargs.kwargs.get("errors", {}).get("base") == "invalid_pv_input"


@pytest.mark.asyncio
async def test_pv_subentry_reconfigure_vol_invalid_sets_error():
    """vol.Invalid in PV reconfigure sets errors['base'] (line 468-469)."""
    import voluptuous as vol

    from custom_components.battery_controller.config_flow import (
        BatteryControllerPVSubentryFlow,
    )

    flow = BatteryControllerPVSubentryFlow()
    flow.async_show_form = MagicMock(return_value={"type": "form", "errors": {}})
    flow._get_entry = MagicMock()
    flow._get_reconfigure_subentry = MagicMock()
    flow._get_reconfigure_subentry.return_value.data = {"peak_power_kwp": 4.0}

    with patch(
        "custom_components.battery_controller.config_flow._validate_pv_subentry",
        side_effect=vol.Invalid("bad pv"),
    ):
        await flow.async_step_reconfigure(user_input={"peak_power_kwp": 4.0})

    call_kwargs = flow.async_show_form.call_args
    assert call_kwargs.kwargs.get("errors", {}).get("base") == "invalid_pv_input"


# ---------------------------------------------------------------------------
# Options flow
# ---------------------------------------------------------------------------


async def test_options_flow_sensor_change(
    hass: HomeAssistant,
    v4_config_entry: config_entries.ConfigEntry,
) -> None:
    """Price sensor can be changed via options flow."""
    result = await hass.config_entries.options.async_init(v4_config_entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            "sensors": {
                "price_sensor": "sensor.new_price_sensor",
            },
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert v4_config_entry.options["price_sensor"] == "sensor.new_price_sensor"


async def test_options_flow_preserves_number_entity_values(
    hass: HomeAssistant,
    v4_config_entry: config_entries.ConfigEntry,
) -> None:
    """Number entity values in options (degradation, spread) survive an options round-trip."""
    hass.config_entries.async_update_entry(
        v4_config_entry,
        options={"degradation_cost_per_cycle": 0.025, "min_price_spread": 0.08},
    )

    result = await hass.config_entries.options.async_init(v4_config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            "sensors": {"price_sensor": "sensor.price"},
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert v4_config_entry.options["degradation_cost_per_cycle"] == 0.025
    assert v4_config_entry.options["min_price_spread"] == 0.08


async def test_options_flow_subentries_untouched(
    hass: HomeAssistant,
    v4_config_entry_with_battery: config_entries.ConfigEntry,
    pv_subentry_data: dict,
) -> None:
    """Options flow does not affect subentries."""
    entry = v4_config_entry_with_battery
    # Add a PV subentry too
    from homeassistant.config_entries import ConfigSubentry

    pv_sub = ConfigSubentry(
        subentry_type=PV_SUBENTRY_TYPE,
        title="4 kWp AC",
        data=pv_subentry_data,
        unique_id=None,
    )
    hass.config_entries.async_add_subentry(entry, pv_sub)
    subentry_ids_before = set(entry.subentries.keys())

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={"sensors": {"price_sensor": "sensor.price"}},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert set(entry.subentries.keys()) == subentry_ids_before


async def test_options_flow_missing_price_sensor_preserves_existing_options(
    hass: HomeAssistant,
    v4_config_entry: config_entries.ConfigEntry,
) -> None:
    """A failed options submit must not overwrite existing options."""
    original_options = {
        "price_sensor": "sensor.original_price",
        "degradation_cost_per_cycle": 0.025,
        "min_price_spread": 0.08,
        "zero_grid_deadband_w": 50.0,
    }
    hass.config_entries.async_update_entry(v4_config_entry, options=original_options)

    result = await hass.config_entries.options.async_init(v4_config_entry.entry_id)
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(
            result["flow_id"],
            user_input={"sensors": {}},
        )
    assert v4_config_entry.options == original_options


async def test_options_flow_init_preserves_existing_options_until_submit(
    hass: HomeAssistant,
    v4_config_entry: config_entries.ConfigEntry,
) -> None:
    """Opening the options flow must not mutate existing option values."""
    hass.config_entries.async_update_entry(
        v4_config_entry,
        data={
            "price_sensor": "sensor.data_price",
            "fixed_feed_in_price": 0.04,
            "zero_grid_enabled": False,
            "max_grid_power_kw": 3.0,
        },
        options={
            "price_sensor": "sensor.option_price",
            "fixed_feed_in_price": 0.09,
            "zero_grid_enabled": True,
            "max_grid_power_kw": 7.5,
        },
    )

    result = await hass.config_entries.options.async_init(v4_config_entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    assert v4_config_entry.options["price_sensor"] == "sensor.option_price"
    assert v4_config_entry.options["fixed_feed_in_price"] == 0.09
    assert v4_config_entry.options["zero_grid_enabled"] is True
    assert v4_config_entry.options["max_grid_power_kw"] == 7.5


async def test_options_flow_partial_submit_applies_defaults_and_empty_lists(
    hass: HomeAssistant,
    v4_config_entry: config_entries.ConfigEntry,
) -> None:
    """A minimal valid submit should normalize omitted sections consistently."""
    result = await hass.config_entries.options.async_init(v4_config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={"sensors": {"price_sensor": "sensor.updated_price"}},
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert v4_config_entry.options["price_sensor"] == "sensor.updated_price"
    assert v4_config_entry.options["feed_in_price_sensor"] is None
    assert v4_config_entry.options["power_consumption_sensors"] == []
    assert v4_config_entry.options["power_production_sensors"] == []
    assert v4_config_entry.options["electricity_consumption_sensors"] == []
    assert v4_config_entry.options["electricity_production_sensors"] == []
    assert v4_config_entry.options["pv_production_sensors"] == []
    assert v4_config_entry.options["fixed_feed_in_price"] == 0.04
    assert v4_config_entry.options["zero_grid_enabled"] is True
    assert v4_config_entry.options["zero_grid_response_time_s"] == 10.0
    assert v4_config_entry.options["max_grid_power_kw"] == 0.0

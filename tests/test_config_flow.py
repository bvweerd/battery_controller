"""Tests for the Battery Controller config flow and PV subentry flow."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from homeassistant import config_entries, setup
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData

from custom_components.battery_controller.const import DOMAIN, PV_SUBENTRY_TYPE


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
async def setup_ha(hass: HomeAssistant) -> None:
    """Set up Home Assistant for testing."""
    await setup.async_setup_component(hass, "persistent_notification", {})


@pytest.fixture
def mock_config() -> dict:
    """Minimal valid config flow input (sections layout)."""
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
def v3_config_entry(hass: HomeAssistant) -> config_entries.ConfigEntry:
    """A v3 config entry with no PV subentries."""
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
def v3_config_entry_with_subentries(
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


# ---------------------------------------------------------------------------
# Config flow — initial setup
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
    """Successful config flow creates an entry directly (no PV menu)."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with patch(
        "custom_components.battery_controller.async_setup_entry", return_value=True
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=mock_config
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Battery Controller"
    # No PV arrays in data — those are subentries
    assert "pv_peak_power_kwp" not in result["data"]
    assert "pv_extra_arrays" not in result["data"]


async def test_form_user_stores_battery_fields(
    hass: HomeAssistant, mock_config: dict
) -> None:
    """Battery fields are correctly stored in entry data."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    with patch(
        "custom_components.battery_controller.async_setup_entry", return_value=True
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=mock_config
        )
    assert result["data"]["capacity_kwh"] == 10.0
    assert result["data"]["max_charge_power_kw"] == 5.0
    assert result["data"]["price_sensor"] == "sensor.nordpool_kwh_se3_eur"
    assert result["data"]["battery_soc_sensor"] == "sensor.battery_soc"


async def test_form_user_missing_price_sensor(hass: HomeAssistant) -> None:
    """Missing price_sensor raises InvalidData."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
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
                "sensors": {"battery_soc_sensor": "sensor.battery_soc"},
            },
        )
    assert "price_sensor" in str(exc_info.value)


async def test_form_user_missing_soc_sensor(hass: HomeAssistant) -> None:
    """Missing battery_soc_sensor raises InvalidData."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
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
                "sensors": {"price_sensor": "sensor.nordpool_kwh_se3_eur"},
            },
        )
    assert "battery_soc_sensor" in str(exc_info.value)


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


# ---------------------------------------------------------------------------
# PV subentry flow — add
# ---------------------------------------------------------------------------


async def _init_subentry_add_flow(hass: HomeAssistant, entry_id: str) -> dict:
    """Helper: start a subentry add flow and return the first result."""
    return await hass.config_entries.subentries.async_init(
        (entry_id, PV_SUBENTRY_TYPE),
        context={"source": "user"},
    )


async def test_subentry_add_ac_array(
    hass: HomeAssistant,
    v3_config_entry: config_entries.ConfigEntry,
    pv_subentry_data: dict,
) -> None:
    """Adding an AC PV array creates a subentry with correct data."""
    result = await _init_subentry_add_flow(hass, v3_config_entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input=pv_subentry_data
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert len(v3_config_entry.subentries) == 1
    sub = list(v3_config_entry.subentries.values())[0]
    assert sub.data["peak_power_kwp"] == 4.0
    assert sub.data["dc_coupled"] is False
    assert "AC" in sub.title


async def test_subentry_add_dc_coupled_array(
    hass: HomeAssistant,
    v3_config_entry: config_entries.ConfigEntry,
) -> None:
    """Adding a DC-coupled PV array is stored with dc_coupled=True."""
    result = await _init_subentry_add_flow(hass, v3_config_entry.entry_id)
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
    sub = list(v3_config_entry.subentries.values())[0]
    assert sub.data["dc_coupled"] is True
    assert "DC" in sub.title


async def test_subentry_add_multiple_arrays(
    hass: HomeAssistant,
    v3_config_entry: config_entries.ConfigEntry,
    pv_subentry_data: dict,
) -> None:
    """Three arrays added sequentially → three independent subentries."""
    for orientation in [180.0, 90.0, 270.0]:
        result = await _init_subentry_add_flow(hass, v3_config_entry.entry_id)
        await hass.config_entries.subentries.async_configure(
            result["flow_id"],
            user_input={**pv_subentry_data, "orientation": orientation},
        )

    assert len(v3_config_entry.subentries) == 3
    orientations = {s.data["orientation"] for s in v3_config_entry.subentries.values()}
    assert orientations == {180.0, 90.0, 270.0}


async def test_subentry_add_zero_kwp_rejected(
    hass: HomeAssistant,
    v3_config_entry: config_entries.ConfigEntry,
) -> None:
    """peak_power_kwp=0.0 fails voluptuous validation."""
    result = await _init_subentry_add_flow(hass, v3_config_entry.entry_id)
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
    # No subentry should have been created
    assert len(v3_config_entry.subentries) == 0


async def test_subentry_add_negative_kwp_rejected(
    hass: HomeAssistant,
    v3_config_entry: config_entries.ConfigEntry,
) -> None:
    """Negative peak_power_kwp is rejected."""
    result = await _init_subentry_add_flow(hass, v3_config_entry.entry_id)
    with pytest.raises(InvalidData):
        await hass.config_entries.subentries.async_configure(
            result["flow_id"],
            user_input={
                "peak_power_kwp": -1.0,
                "orientation": 180.0,
                "tilt": 35.0,
                "efficiency_factor": 0.85,
                "dc_coupled": False,
            },
        )
    assert len(v3_config_entry.subentries) == 0


async def test_subentry_add_invalid_orientation_rejected(
    hass: HomeAssistant,
    v3_config_entry: config_entries.ConfigEntry,
) -> None:
    """Orientation outside 0-360 range is rejected."""
    result = await _init_subentry_add_flow(hass, v3_config_entry.entry_id)
    with pytest.raises(InvalidData):
        await hass.config_entries.subentries.async_configure(
            result["flow_id"],
            user_input={
                "peak_power_kwp": 4.0,
                "orientation": 400.0,  # invalid
                "tilt": 35.0,
                "efficiency_factor": 0.85,
                "dc_coupled": False,
            },
        )
    assert len(v3_config_entry.subentries) == 0


async def test_subentry_add_mixed_ac_dc(
    hass: HomeAssistant,
    v3_config_entry: config_entries.ConfigEntry,
    pv_subentry_data: dict,
) -> None:
    """Mix of AC and DC subentries are stored independently."""
    # AC array
    result = await _init_subentry_add_flow(hass, v3_config_entry.entry_id)
    await hass.config_entries.subentries.async_configure(
        result["flow_id"], user_input=pv_subentry_data
    )
    # DC array
    result = await _init_subentry_add_flow(hass, v3_config_entry.entry_id)
    await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input={**pv_subentry_data, "dc_coupled": True, "peak_power_kwp": 2.0},
    )

    assert len(v3_config_entry.subentries) == 2
    dc_count = sum(
        1 for s in v3_config_entry.subentries.values() if s.data["dc_coupled"]
    )
    ac_count = sum(
        1 for s in v3_config_entry.subentries.values() if not s.data["dc_coupled"]
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
    """Reconfiguring a subentry updates its data."""
    entry = v3_config_entry_with_subentries
    subentry_id = list(entry.subentries.keys())[0]
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


async def test_subentry_reconfigure_change_dc_coupling(
    hass: HomeAssistant,
    v3_config_entry_with_subentries: config_entries.ConfigEntry,
) -> None:
    """An AC array can be changed to DC-coupled via reconfigure."""
    entry = v3_config_entry_with_subentries
    # Find the AC subentry (dc_coupled=False)
    ac_id = next(
        sid for sid, sub in entry.subentries.items() if not sub.data["dc_coupled"]
    )

    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, PV_SUBENTRY_TYPE),
        context={"source": "reconfigure", "subentry_id": ac_id},
    )
    await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        user_input={
            "peak_power_kwp": 4.0,
            "orientation": 180.0,
            "tilt": 35.0,
            "efficiency_factor": 0.97,
            "dc_coupled": True,
        },
    )
    assert entry.subentries[ac_id].data["dc_coupled"] is True
    assert "DC" in entry.subentries[ac_id].title


async def test_subentry_reconfigure_leaves_others_unchanged(
    hass: HomeAssistant,
    v3_config_entry_with_subentries: config_entries.ConfigEntry,
) -> None:
    """Editing one subentry does not touch the other."""
    entry = v3_config_entry_with_subentries
    ids = list(entry.subentries.keys())
    target_id, other_id = ids[0], ids[1]
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
# PV subentry — delete
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


async def test_subentry_delete_last(
    hass: HomeAssistant,
    v3_config_entry: config_entries.ConfigEntry,
    pv_subentry_data: dict,
) -> None:
    """Deleting the only subentry leaves the entry with no subentries."""
    from homeassistant.config_entries import ConfigSubentry

    sub = ConfigSubentry(
        subentry_type=PV_SUBENTRY_TYPE,
        title="Only Array",
        data=pv_subentry_data,
        unique_id=None,
    )
    hass.config_entries.async_add_subentry(v3_config_entry, sub)
    assert len(v3_config_entry.subentries) == 1

    hass.config_entries.async_remove_subentry(v3_config_entry, sub.subentry_id)
    assert len(v3_config_entry.subentries) == 0


# ---------------------------------------------------------------------------
# Options flow
# ---------------------------------------------------------------------------


async def test_options_flow_battery_change(
    hass: HomeAssistant,
    v3_config_entry_with_subentries: config_entries.ConfigEntry,
) -> None:
    """Options flow updates battery settings; subentries are untouched."""
    entry = v3_config_entry_with_subentries
    subentry_ids_before = set(entry.subentries.keys())

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            "battery": {
                "capacity_kwh": 15.0,
                "max_charge_power_kw": 7.5,
                "max_discharge_power_kw": 7.5,
                "round_trip_efficiency": 0.92,
            },
            "sensors": {
                "price_sensor": "sensor.nordpool_kwh_se3_eur",
                "battery_soc_sensor": "sensor.battery_soc",
            },
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options["capacity_kwh"] == 15.0
    assert entry.options["max_charge_power_kw"] == 7.5
    # Subentries must not be affected by options flow
    assert set(entry.subentries.keys()) == subentry_ids_before


async def test_options_flow_sensor_change(
    hass: HomeAssistant,
    v3_config_entry: config_entries.ConfigEntry,
) -> None:
    """Price sensor can be changed via options flow."""
    result = await hass.config_entries.options.async_init(v3_config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            "battery": {
                "capacity_kwh": 10.0,
                "max_charge_power_kw": 5.0,
                "max_discharge_power_kw": 5.0,
                "round_trip_efficiency": 0.9,
            },
            "sensors": {
                "price_sensor": "sensor.new_price_sensor",
                "battery_soc_sensor": "sensor.battery_soc",
            },
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert v3_config_entry.options["price_sensor"] == "sensor.new_price_sensor"


async def test_options_flow_preserves_number_entity_values(
    hass: HomeAssistant,
    v3_config_entry: config_entries.ConfigEntry,
) -> None:
    """Number entity values in options (min_soc etc.) survive an options flow round-trip."""

    # Simulate number entity having written min_soc_percent to options
    hass.config_entries.async_update_entry(
        v3_config_entry,
        options={"min_soc_percent": 15.0, "max_soc_percent": 85.0},
    )

    result = await hass.config_entries.options.async_init(v3_config_entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={
            "battery": {
                "capacity_kwh": 10.0,
                "max_charge_power_kw": 5.0,
                "max_discharge_power_kw": 5.0,
                "round_trip_efficiency": 0.9,
            },
            "sensors": {
                "price_sensor": "sensor.price",
                "battery_soc_sensor": "sensor.soc",
            },
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert v3_config_entry.options["min_soc_percent"] == 15.0
    assert v3_config_entry.options["max_soc_percent"] == 85.0

"""Tests for the Battery Controller config flow, PV subentry flow, and battery subentry flow."""

from __future__ import annotations

from unittest.mock import patch

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
    with patch(
        "custom_components.battery_controller.async_setup_entry", return_value=True
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

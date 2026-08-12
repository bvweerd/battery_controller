"""Tests for the Battery Controller diagnostics."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from custom_components.battery_controller import const
from custom_components.battery_controller.diagnostics import (
    TO_REDACT,
    async_get_config_entry_diagnostics,
)
from custom_components.battery_controller.__init__ import BatteryControllerData


def test_every_sensor_config_key_is_redacted() -> None:
    """No config key holding an entity ID may leak into diagnostics.

    Diagnostics are routinely pasted into public issue trackers, and the redact
    list is maintained by hand, so it silently fell behind: it once carried a
    key name that did not exist while eight real ones were missing. Derive the
    expectation from const.py so adding a sensor option cannot repeat that.
    """
    sensor_keys = {
        value
        for name, value in vars(const).items()
        if name.startswith("CONF_")
        and isinstance(value, str)
        and (name.endswith("_SENSOR") or name.endswith("_SENSORS"))
    }
    assert sensor_keys, "no sensor config keys discovered — check the naming rule"
    assert sensor_keys <= TO_REDACT, (
        f"unredacted sensor config keys: {sorted(sensor_keys - TO_REDACT)}"
    )


def test_redact_list_has_no_dead_entries() -> None:
    """Every redacted key must correspond to a real config key."""
    all_conf_values = {
        value
        for name, value in vars(const).items()
        if name.startswith("CONF_") and isinstance(value, str)
    }
    assert TO_REDACT <= all_conf_values, (
        f"redact entries matching no config key: {sorted(TO_REDACT - all_conf_values)}"
    )


@pytest.fixture
def mock_config_entry() -> MagicMock:
    """Mock a config entry."""
    entry = MagicMock(spec=ConfigEntry)
    entry.entry_id = "test_entry_id"
    entry.title = "Test Entry"
    entry.data = {"price_sensor": "sensor.test_price"}
    entry.options = {}
    entry.subentries = {}
    return entry


async def test_diagnostics_dataclass_access(
    hass: HomeAssistant, mock_config_entry: MagicMock
):
    """Test diagnostics works when runtime_data is a dataclass."""

    # Mock coordinators
    weather_coord = MagicMock()
    weather_coord.data = {
        "radiation_forecast": [0.0],
        "timestamp": "2024-01-01T00:00:00",
    }
    weather_coord.last_update_success = True

    forecast_coord = MagicMock()
    forecast_coord.data = {"pv_forecast_kw": [0.0], "timestamp": "2024-01-01T00:00:00"}
    forecast_coord.last_update_success = True
    # Mock consumption model with a dictionary pattern (to test the dict-key-tuple fix)
    model = MagicMock()
    model._hourly_pattern = {(12, 0): 0.5}
    forecast_coord.consumption_model = model

    optimization_coord = MagicMock()
    optimization_coord.data = {
        "control_mode": "hybrid",
        "optimal_mode": "idle",
        "optimal_power_kw": 0.0,
        "control_action": {"target_power_kw": 0.0, "action_mode": "idle"},
        "battery_setpoints": {"bat1": 0.0},
        "raw_total_cost": -1.23,
        "raw_savings": 0.45,
        "timestamp": "2024-01-01T00:00:00",
        "optimization_result": MagicMock(
            power_schedule_kw=[0.0],
            mode_schedule=["idle"],
            soc_schedule_kwh=[5.0],
            price_forecast=[0.20],
            pv_forecast=[0.0],
            consumption_forecast=[0.5],
        ),
        "battery_state": MagicMock(
            soc_kwh=5.0, soc_percent=50.0, power_kw=0.0, mode="idle"
        ),
    }
    optimization_coord.last_update_success = True
    optimization_coord._committed_action = "charging"
    optimization_coord._committed_power = 1.2
    optimization_coord._committed_price = 0.25
    optimization_coord._committed_step_start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    optimization_coord.battery_config = MagicMock(
        capacity_kwh=10.0,
        usable_capacity_kwh=8.0,
        max_charge_power_kw=5.0,
        max_discharge_power_kw=5.0,
        round_trip_efficiency=0.9,
        charge_efficiency=0.95,
        discharge_efficiency=0.95,
        min_soc_percent=10.0,
        max_soc_percent=90.0,
        min_soc_kwh=1.0,
        max_soc_kwh=9.0,
        pv_dc_coupled=False,
        pv_dc_peak_power_kwp=0.0,
        pv_dc_efficiency=0.97,
    )

    # Set up runtime_data as the dataclass
    mock_config_entry.runtime_data = BatteryControllerData(
        weather_coordinator=weather_coord,
        forecast_coordinator=forecast_coord,
        optimization_coordinator=optimization_coord,
        config={},
        device=MagicMock(),
        battery_devices={},
        pv_devices={},
    )

    # We need to mock entity_registry.async_get and er.async_entries_for_config_entry
    with patch("homeassistant.helpers.entity_registry.async_get") as mock_er_get:
        mock_er_get.return_value = MagicMock()
        with patch(
            "homeassistant.helpers.entity_registry.async_entries_for_config_entry"
        ) as mock_entries:
            mock_entries.return_value = []

            diagnostics = await async_get_config_entry_diagnostics(
                hass, mock_config_entry
            )

    # Verification
    assert diagnostics["config_entry"]["entry_id"] == "test_entry_id"
    assert diagnostics["weather"]["last_update_success"] is True
    # Data age is reported so stale-but-"successful" coordinators are visible
    assert diagnostics["weather"]["age_minutes"] is not None
    assert diagnostics["weather"]["stale"] is True  # 2024 timestamp is long stale
    assert diagnostics["forecast"]["age_minutes"] is not None
    assert diagnostics["forecast"]["consumption_hourly_pattern"] == {"12_0": 0.5}
    assert diagnostics["optimization"]["control_mode"] == "hybrid"
    assert diagnostics["optimization"]["control_action"]["target_power_kw"] == 0.0
    assert diagnostics["optimization"]["battery_setpoints"] == {"bat1": 0.0}
    assert diagnostics["optimization"]["commitment_state"]["action"] == "charging"
    assert diagnostics["optimization"]["commitment_state"]["power_kw"] == 1.2
    assert diagnostics["optimization"]["raw_total_cost"] == -1.23
    assert diagnostics["optimization"]["raw_savings"] == 0.45
    assert diagnostics["optimization"]["schedule"]["power_schedule_kw"] == [0.0]
    assert diagnostics["optimization"]["battery_state"]["mode"] == "idle"


async def test_diagnostics_missing_runtime_data(
    hass: HomeAssistant, mock_config_entry: MagicMock
):
    """Test diagnostics works when runtime_data is missing."""
    if hasattr(mock_config_entry, "runtime_data"):
        del mock_config_entry.runtime_data

    # We need to mock entity_registry.async_get and er.async_entries_for_config_entry
    with patch("homeassistant.helpers.entity_registry.async_get") as mock_er_get:
        mock_er_get.return_value = MagicMock()
        with patch(
            "homeassistant.helpers.entity_registry.async_entries_for_config_entry"
        ) as mock_entries:
            mock_entries.return_value = []

            diagnostics = await async_get_config_entry_diagnostics(
                hass, mock_config_entry
            )

    # Verification
    assert diagnostics["config_entry"]["entry_id"] == "test_entry_id"
    assert diagnostics["weather"] == {}
    assert diagnostics["forecast"] == {}
    assert diagnostics["optimization"] == {}


async def test_diagnostics_entity_state_none(
    hass: HomeAssistant, mock_config_entry: MagicMock
):
    """Test diagnostics when hass.states.get returns None for an entity."""
    mock_config_entry.runtime_data = None
    if hasattr(mock_config_entry, "runtime_data"):
        del mock_config_entry.runtime_data

    ent_entry = MagicMock()
    ent_entry.entity_id = "sensor.test_sensor"
    ent_entry.unique_id = "unique_123"

    with (
        patch("homeassistant.helpers.entity_registry.async_get") as mock_er_get,
        patch(
            "homeassistant.helpers.entity_registry.async_entries_for_config_entry"
        ) as mock_entries,
        patch(
            "homeassistant.core.StateMachine.get",
            return_value=None,
        ),
    ):
        mock_er_get.return_value = MagicMock()
        mock_entries.return_value = [ent_entry]

        diagnostics = await async_get_config_entry_diagnostics(hass, mock_config_entry)

    assert diagnostics["entities"][0]["state"] is None
    assert diagnostics["entities"][0]["attributes"] == {}

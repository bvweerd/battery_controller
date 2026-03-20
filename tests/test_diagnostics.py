"""Tests for the Battery Controller diagnostics."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from custom_components.battery_controller.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.battery_controller.__init__ import BatteryControllerData


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
        "raw_shadow_price_eur_kwh": 0.33,
        "shadow_price_source": "raw_dp",
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
    optimization_coord._committed_step_start = "2024-01-01T00:00:00+00:00"
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
    assert diagnostics["forecast"]["consumption_hourly_pattern"] == {"12_0": 0.5}
    assert diagnostics["optimization"]["control_mode"] == "hybrid"
    assert diagnostics["optimization"]["control_action"]["target_power_kw"] == 0.0
    assert diagnostics["optimization"]["battery_setpoints"] == {"bat1": 0.0}
    assert diagnostics["optimization"]["commitment_state"]["action"] == "charging"
    assert diagnostics["optimization"]["commitment_state"]["power_kw"] == 1.2
    assert diagnostics["optimization"]["raw_total_cost"] == -1.23
    assert diagnostics["optimization"]["raw_savings"] == 0.45
    assert diagnostics["optimization"]["raw_shadow_price_eur_kwh"] == 0.33
    assert diagnostics["optimization"]["shadow_price_source"] == "raw_dp"
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

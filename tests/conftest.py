"""Conftest for Battery Controller tests."""

from __future__ import annotations

import pytest

from custom_components.battery_controller.battery_model import BatteryConfig


# Enable loading of custom integrations for all tests
@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Automatically enable custom integration."""
    return enable_custom_integrations


@pytest.fixture
def standard_battery_config() -> BatteryConfig:
    """Standard 10 kWh / 5 kW battery used across many tests."""
    return BatteryConfig(
        capacity_kwh=10.0,
        max_charge_power_kw=5.0,
        max_discharge_power_kw=5.0,
        round_trip_efficiency=0.90,
        min_soc_percent=10.0,
        max_soc_percent=90.0,
    )

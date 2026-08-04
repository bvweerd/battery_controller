"""Conftest for Battery Controller tests."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from custom_components.battery_controller.battery_model import BatteryConfig
from custom_components.battery_controller.const import (
    CONF_BATTERY_SOC_SENSOR,
    CONF_CONTROL_MODE,
    CONF_FIXED_FEED_IN_PRICE,
    CONF_MAX_CHARGE_POWER_KW,
    CONF_MAX_DISCHARGE_POWER_KW,
    CONF_MAX_SOC_PERCENT,
    CONF_MIN_SOC_PERCENT,
    CONF_POWER_CONSUMPTION_SENSORS,
    CONF_POWER_PRODUCTION_SENSORS,
    CONF_PRICE_SENSOR,
    MODE_FOLLOW_SCHEDULE,
)
from custom_components.battery_controller.coordinator_optimization import (
    OptimizationCoordinator,
)


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
        charge_efficiency_curve="0.9487",
        discharge_efficiency_curve="0.9487",
        min_soc_percent=10.0,
        max_soc_percent=90.0,
    )


@pytest.fixture
def optimization_coordinator(hass):
    """A minimal OptimizationCoordinator with one battery subentry.

    Enough to exercise the coordinator's own helpers without standing up
    forecasts, price sensors or an optimizer run.
    """
    weather_coordinator = MagicMock()
    weather_coordinator.data = {}
    forecast_coordinator = MagicMock()
    forecast_coordinator.data = None
    forecast_coordinator.async_add_listener = MagicMock(return_value=lambda: None)
    config = {
        "entry_id": "test-entry",
        CONF_PRICE_SENSOR: "sensor.test_price",
        CONF_CONTROL_MODE: MODE_FOLLOW_SCHEDULE,
        CONF_FIXED_FEED_IN_PRICE: 0.07,
        CONF_POWER_CONSUMPTION_SENSORS: [],
        CONF_POWER_PRODUCTION_SENSORS: [],
        "battery_subentries": [
            (
                "bat1",
                {
                    CONF_MAX_CHARGE_POWER_KW: 5.0,
                    CONF_MAX_DISCHARGE_POWER_KW: 5.0,
                    CONF_MIN_SOC_PERCENT: 10.0,
                    CONF_MAX_SOC_PERCENT: 90.0,
                    CONF_BATTERY_SOC_SENSOR: "sensor.test_soc",
                },
            )
        ],
    }
    return OptimizationCoordinator(
        hass, weather_coordinator, forecast_coordinator, config
    )

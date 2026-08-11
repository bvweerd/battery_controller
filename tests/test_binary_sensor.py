"""Tests for binary_sensor platform."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from custom_components.battery_controller.binary_sensor import async_setup_entry
from homeassistant.helpers.entity import DeviceInfo


from custom_components.battery_controller.binary_sensor import (
    PVCurtailmentSensor,
    UseMaxPowerSensor,
)
from custom_components.battery_controller.const import DOMAIN


def _make_coord(data=None):
    coord = MagicMock()
    coord.data = data
    coord.battery_config = MagicMock()
    coord.battery_config.max_soc_kwh = 10.0
    return coord


def _make_entry(entry_id="test_entry"):
    entry = MagicMock()
    entry.entry_id = entry_id
    return entry


def _make_device():

    return DeviceInfo(identifiers={(DOMAIN, "test_entry")})


# ---------------------------------------------------------------------------
# PVCurtailmentSensor
# ---------------------------------------------------------------------------


class TestPVCurtailmentSensor:
    def _make_sensor(self, data=None):
        coord = _make_coord(data)
        entry = _make_entry()
        device = _make_device()
        return PVCurtailmentSensor(coord, device, entry)

    def test_is_on_none_when_no_data(self):
        sensor = self._make_sensor(data=None)
        assert sensor.is_on is None

    def test_is_on_false_when_feed_in_price_zero(self):
        sensor = self._make_sensor(
            data={
                "current_feed_in_price": 0.0,
                "battery_state": None,
                "control_action": {},
            }
        )
        assert sensor.is_on is False

    def test_is_on_false_when_feed_in_price_positive(self):
        sensor = self._make_sensor(
            data={
                "current_feed_in_price": 0.07,
                "battery_state": None,
                "control_action": {},
            }
        )
        assert sensor.is_on is False

    def test_is_on_true_when_battery_state_none_and_negative_price(self):
        sensor = self._make_sensor(
            data={
                "current_feed_in_price": -0.05,
                "battery_state": None,
                "control_action": {},
            }
        )
        assert sensor.is_on is True

    def test_is_on_true_when_price_negative_regardless_of_battery_state(self):
        """Purely price-based: negative price = ON, even while the battery is
        absorbing the full setpoint or has plenty of SoC headroom."""
        battery_state = MagicMock()
        battery_state.soc_kwh = 5.0  # well below max
        battery_state.power_kw = 1.0  # absorbing the full 1000W setpoint
        sensor = self._make_sensor(
            data={
                "current_feed_in_price": -0.05,
                "battery_state": battery_state,
                "control_action": {"target_power_w": 1000.0},
            }
        )
        assert sensor.is_on is True

    def test_is_on_true_when_soc_at_max_and_price_negative(self):
        battery_state = MagicMock()
        battery_state.soc_kwh = 9.85  # 98.5% of 10.0 kWh
        battery_state.power_kw = -0.5
        sensor = self._make_sensor(
            data={
                "current_feed_in_price": -0.05,
                "battery_state": battery_state,
                "control_action": {"target_power_w": 500.0},
            }
        )
        assert sensor.is_on is True

    def test_follows_price_sign_flips(self):
        """The sensor tracks the price sign directly, with no latched state."""
        battery_state = MagicMock()
        battery_state.soc_kwh = 5.0
        battery_state.power_kw = 0.75
        sensor = self._make_sensor(
            data={
                "current_feed_in_price": -0.05,
                "battery_state": battery_state,
                "control_action": {"target_power_w": 1000.0},
            }
        )
        assert sensor.is_on is True

        sensor.coordinator.data["current_feed_in_price"] = 0.10
        assert sensor.is_on is False

        sensor.coordinator.data["current_feed_in_price"] = -0.02
        assert sensor.is_on is True

    def test_extra_state_attributes_empty_when_no_data(self):
        sensor = self._make_sensor(data=None)
        assert sensor.extra_state_attributes == {}

    def test_extra_state_attributes_without_battery_state(self):
        sensor = self._make_sensor(
            data={
                "current_feed_in_price": 0.07,
                "battery_state": None,
                "control_action": {},
            }
        )
        attrs = sensor.extra_state_attributes
        assert "current_feed_in_price" in attrs
        assert attrs["current_feed_in_price"] == 0.07
        assert "battery_soc_percent" not in attrs

    def test_extra_state_attributes_with_battery_state(self):
        battery_state = MagicMock()
        battery_state.soc_percent = 75.5
        battery_state.power_kw = -1.2
        sensor = self._make_sensor(
            data={
                "current_feed_in_price": -0.01,
                "battery_state": battery_state,
                "control_action": {"target_power_w": 500.0},
            }
        )
        attrs = sensor.extra_state_attributes
        assert attrs["battery_soc_percent"] == 75.5
        assert attrs["battery_power_kw"] == round(-1.2, 3)
        assert attrs["charge_setpoint_w"] == 500.0


# ---------------------------------------------------------------------------
# UseMaxPowerSensor
# ---------------------------------------------------------------------------


class TestUseMaxPowerSensor:
    def _make_sensor(self, data=None):
        coord = _make_coord(data)
        entry = _make_entry()
        device = _make_device()
        return UseMaxPowerSensor(coord, device, entry)

    def test_is_on_none_when_no_data(self):
        sensor = self._make_sensor(data=None)
        assert sensor.is_on is None

    def test_is_on_true_when_price_negative(self):
        sensor = self._make_sensor(data={"current_price": -0.05})
        assert sensor.is_on is True

    def test_is_on_false_when_price_zero(self):
        sensor = self._make_sensor(data={"current_price": 0.0})
        assert sensor.is_on is False

    def test_is_on_false_when_price_positive(self):
        sensor = self._make_sensor(data={"current_price": 0.25})
        assert sensor.is_on is False

    def test_extra_state_attributes_empty_when_no_data(self):
        sensor = self._make_sensor(data=None)
        assert sensor.extra_state_attributes == {}

    def test_extra_state_attributes_with_buy_price(self):
        sensor = self._make_sensor(data={"current_price": 0.22})
        attrs = sensor.extra_state_attributes
        assert attrs["current_buy_price"] == 0.22

    def test_unique_id_contains_key(self):
        sensor = self._make_sensor(data={})
        assert "use_max_power" in sensor._attr_unique_id

    def test_pv_curtailment_unique_id(self):
        coord = _make_coord({})
        entry = _make_entry("eid123")
        device = _make_device()
        sensor = PVCurtailmentSensor(coord, device, entry)
        assert sensor._attr_unique_id == "eid123_pv_curtailment"


# ---------------------------------------------------------------------------
# async_setup_entry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_setup_entry_adds_entities():
    """async_setup_entry reads runtime_data and calls async_add_entities."""

    coord = _make_coord()
    device = _make_device()

    runtime_data = MagicMock()
    runtime_data.optimization_coordinator = coord
    runtime_data.device = device

    entry = MagicMock()
    entry.runtime_data = runtime_data

    added = []
    async_add_entities = MagicMock(side_effect=lambda entities: added.extend(entities))

    await async_setup_entry(MagicMock(), entry, async_add_entities)

    async_add_entities.assert_called_once()
    assert len(added) == 2


class TestPVCurtailmentMissingControlAction:
    """M4: target_power_w key absent from control_action dict."""

    def _make_sensor(self, data=None):
        coord = _make_coord(data)
        entry = _make_entry()
        device = _make_device()
        return PVCurtailmentSensor(coord, device, entry)

    def test_missing_target_power_w_does_not_raise(self):
        """control_action dict with no target_power_w key must not break is_on."""
        # Purely price-based: negative price → ON regardless of control_action.
        sensor = self._make_sensor(
            data={
                "current_feed_in_price": -0.05,
                "battery_state": MagicMock(soc_kwh=5.0, power_kw=1.0, soc_percent=50.0),
                "control_action": {},  # no target_power_w key
            }
        )
        sensor.coordinator.battery_config.max_soc_kwh = 10.0
        assert sensor.is_on is True

    def test_none_control_action_does_not_raise(self):
        """control_action absent from data dict entirely must not break is_on."""
        sensor = self._make_sensor(
            data={
                "current_feed_in_price": -0.05,
                "battery_state": MagicMock(soc_kwh=5.0, power_kw=1.0, soc_percent=50.0),
                # no control_action key at all
            }
        )
        sensor.coordinator.battery_config.max_soc_kwh = 10.0
        assert sensor.is_on is True


@pytest.mark.asyncio
async def test_async_setup_entry_none_runtime_data():
    """async_setup_entry returns early (no entities) when runtime_data is None."""

    entry = MagicMock()
    entry.runtime_data = None

    async_add_entities = MagicMock()
    await async_setup_entry(MagicMock(), entry, async_add_entities)

    async_add_entities.assert_not_called()

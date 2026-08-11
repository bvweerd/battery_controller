"""Tests for number platform."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from custom_components.battery_controller.const import (
    CONF_DEGRADATION_COST_PER_CYCLE,
    CONF_MANUAL_POWER_SETPOINT_W,
    CONF_MAX_CHARGE_POWER_KW,
    CONF_MAX_DISCHARGE_POWER_KW,
    CONF_MIN_PRICE_SPREAD,
    CONF_ZERO_GRID_DEADBAND_W,
    DEFAULT_DEGRADATION_COST_PER_CYCLE,
    DEFAULT_MANUAL_POWER_SETPOINT_W,
    DEFAULT_MAX_CHARGE_POWER_KW,
    DEFAULT_MAX_DISCHARGE_POWER_KW,
    DEFAULT_MIN_PRICE_SPREAD,
    DEFAULT_ZERO_GRID_DEADBAND_W,
    DOMAIN,
)
from custom_components.battery_controller.number import (
    DegradationCostNumber,
    ManualPowerSetpointNumber,
    MinPriceSpreadNumber,
    ZeroGridDeadbandNumber,
)
from custom_components.battery_controller.number import async_setup_entry
from homeassistant.helpers.entity import DeviceInfo


def _make_hass():
    hass = MagicMock()
    hass.config_entries = MagicMock()
    hass.config_entries.async_update_entry = MagicMock()
    return hass


def _make_entry(entry_id="test_entry", options=None, data=None):
    entry = MagicMock()
    entry.entry_id = entry_id
    entry.options = options or {}
    entry.data = data or {}
    return entry


def _make_device():

    return DeviceInfo(identifiers={(DOMAIN, "test_entry")})


# ---------------------------------------------------------------------------
# DegradationCostNumber
# ---------------------------------------------------------------------------


class TestDegradationCostNumber:
    def _make(self, options=None, data=None):
        hass = _make_hass()
        entry = _make_entry(options=options, data=data)
        device = _make_device()
        return DegradationCostNumber(hass, entry, device, {})

    def test_unique_id(self):
        n = self._make()
        assert "degradation_cost" in n._attr_unique_id

    def test_native_value_from_default(self):
        n = self._make()
        assert n.native_value == DEFAULT_DEGRADATION_COST_PER_CYCLE

    def test_native_value_from_options(self):
        n = self._make(options={CONF_DEGRADATION_COST_PER_CYCLE: 0.10})
        assert n.native_value == 0.10

    def test_native_value_from_data_when_no_options(self):
        n = self._make(data={CONF_DEGRADATION_COST_PER_CYCLE: 0.08})
        assert n.native_value == 0.08

    def test_options_takes_priority_over_data(self):
        n = self._make(
            options={CONF_DEGRADATION_COST_PER_CYCLE: 0.05},
            data={CONF_DEGRADATION_COST_PER_CYCLE: 0.99},
        )
        assert n.native_value == 0.05

    @pytest.mark.asyncio
    async def test_async_set_native_value(self):
        n = self._make()
        n.async_write_ha_state = MagicMock()
        await n.async_set_native_value(0.07)
        n.hass.config_entries.async_update_entry.assert_called_once()
        n.async_write_ha_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_async_set_native_value_preserves_existing_options(self):
        n = self._make(options={"existing_key": "keep-me"})
        n.async_write_ha_state = MagicMock()

        await n.async_set_native_value(0.07)

        n.hass.config_entries.async_update_entry.assert_called_once_with(
            n._entry,
            options={
                "existing_key": "keep-me",
                CONF_DEGRADATION_COST_PER_CYCLE: 0.07,
            },
        )


# ---------------------------------------------------------------------------
# MinPriceSpreadNumber
# ---------------------------------------------------------------------------


class TestMinPriceSpreadNumber:
    def _make(self, options=None, data=None):
        hass = _make_hass()
        entry = _make_entry(options=options, data=data)
        device = _make_device()
        return MinPriceSpreadNumber(hass, entry, device, {})

    def test_native_value_default(self):
        n = self._make()
        assert n.native_value == DEFAULT_MIN_PRICE_SPREAD

    def test_native_value_from_options(self):
        n = self._make(options={CONF_MIN_PRICE_SPREAD: 0.12})
        assert n.native_value == 0.12

    @pytest.mark.asyncio
    async def test_async_set_native_value(self):
        n = self._make()
        n.async_write_ha_state = MagicMock()
        await n.async_set_native_value(0.08)
        n.hass.config_entries.async_update_entry.assert_called_once()
        n.async_write_ha_state.assert_called_once()

    def test_native_value_none_in_options_falls_back_to_default(self):
        n = self._make(options={CONF_MIN_PRICE_SPREAD: None})
        assert n.native_value == DEFAULT_MIN_PRICE_SPREAD


# ---------------------------------------------------------------------------
# ZeroGridDeadbandNumber
# ---------------------------------------------------------------------------


class TestZeroGridDeadbandNumber:
    def _make(self, options=None, data=None):
        hass = _make_hass()
        entry = _make_entry(options=options, data=data)
        device = _make_device()
        return ZeroGridDeadbandNumber(hass, entry, device, {})

    def test_native_value_default(self):
        n = self._make()
        assert n.native_value == DEFAULT_ZERO_GRID_DEADBAND_W

    def test_native_value_from_options(self):
        n = self._make(options={CONF_ZERO_GRID_DEADBAND_W: 100.0})
        assert n.native_value == 100.0

    @pytest.mark.asyncio
    async def test_async_set_native_value(self):
        n = self._make()
        n.async_write_ha_state = MagicMock()
        await n.async_set_native_value(75.0)
        n.hass.config_entries.async_update_entry.assert_called_once()

    def test_native_value_none_in_data_falls_back_to_default(self):
        n = self._make(data={CONF_ZERO_GRID_DEADBAND_W: None})
        assert n.native_value == DEFAULT_ZERO_GRID_DEADBAND_W


# ---------------------------------------------------------------------------
# ManualPowerSetpointNumber
# ---------------------------------------------------------------------------


class TestManualPowerSetpointNumber:
    def _make(self, config=None, options=None, data=None):
        hass = _make_hass()
        entry = _make_entry(options=options, data=data)
        device = _make_device()
        return ManualPowerSetpointNumber(hass, entry, device, config or {})

    def test_native_value_default(self):
        n = self._make()
        assert n.native_value == DEFAULT_MANUAL_POWER_SETPOINT_W

    def test_native_value_from_options(self):
        n = self._make(options={CONF_MANUAL_POWER_SETPOINT_W: 500.0})
        assert n.native_value == 500.0

    def test_native_min_value_from_config(self):
        n = self._make(config={CONF_MAX_CHARGE_POWER_KW: 5.0})
        assert n.native_min_value == -5000.0

    def test_native_min_value_default(self):
        n = self._make()
        assert n.native_min_value == -DEFAULT_MAX_CHARGE_POWER_KW * 1000

    def test_native_max_value_from_config(self):
        n = self._make(config={CONF_MAX_DISCHARGE_POWER_KW: 3.0})
        assert n.native_max_value == 3000.0

    def test_native_max_value_default(self):
        n = self._make()
        assert n.native_max_value == DEFAULT_MAX_DISCHARGE_POWER_KW * 1000

    def test_native_min_value_invalid_config_falls_back_to_default(self):
        n = self._make(config={CONF_MAX_CHARGE_POWER_KW: "not-a-number"})
        assert n.native_min_value == -DEFAULT_MAX_CHARGE_POWER_KW * 1000

    def test_native_max_value_invalid_config_falls_back_to_default(self):
        n = self._make(config={CONF_MAX_DISCHARGE_POWER_KW: "not-a-number"})
        assert n.native_max_value == DEFAULT_MAX_DISCHARGE_POWER_KW * 1000

    @pytest.mark.asyncio
    async def test_async_set_native_value(self):
        n = self._make()
        n.async_write_ha_state = MagicMock()
        await n.async_set_native_value(1000.0)
        n.hass.config_entries.async_update_entry.assert_called_once()
        n.async_write_ha_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_async_set_native_value_preserves_existing_options(self):
        n = self._make(options={"existing_key": "keep-me"})
        n.async_write_ha_state = MagicMock()

        await n.async_set_native_value(1000.0)

        n.hass.config_entries.async_update_entry.assert_called_once_with(
            n._entry,
            options={
                "existing_key": "keep-me",
                CONF_MANUAL_POWER_SETPOINT_W: 1000.0,
            },
        )

    def test_get_runtime_value_fallback_to_default(self):
        n = self._make(options={}, data={})
        # CONF_MANUAL_POWER_SETPOINT_W not in options or data → default
        assert n.native_value == DEFAULT_MANUAL_POWER_SETPOINT_W


# ---------------------------------------------------------------------------
# async_setup_entry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_setup_entry_adds_entities():
    """async_setup_entry reads runtime_data and calls async_add_entities."""

    hass = _make_hass()
    config = {
        CONF_MAX_CHARGE_POWER_KW: 5.0,
        CONF_MAX_DISCHARGE_POWER_KW: 5.0,
    }
    device = _make_device()

    runtime_data = MagicMock()
    runtime_data.config = config
    runtime_data.device = device

    entry = _make_entry()
    entry.runtime_data = runtime_data

    added = []
    async_add_entities = MagicMock(side_effect=lambda entities: added.extend(entities))

    await async_setup_entry(hass, entry, async_add_entities)

    async_add_entities.assert_called_once()
    assert len(added) == 4


@pytest.mark.asyncio
async def test_async_setup_entry_without_runtime_data_skips_entity_setup():

    entry = _make_entry()
    entry.runtime_data = None
    async_add_entities = MagicMock()

    await async_setup_entry(_make_hass(), entry, async_add_entities)
    async_add_entities.assert_not_called()


@pytest.mark.asyncio
async def test_async_setup_entry_without_config_skips_entity_setup():

    runtime_data = MagicMock()
    runtime_data.device = _make_device()
    runtime_data.config = None

    entry = _make_entry()
    entry.runtime_data = runtime_data
    async_add_entities = MagicMock()

    await async_setup_entry(_make_hass(), entry, async_add_entities)
    async_add_entities.assert_not_called()

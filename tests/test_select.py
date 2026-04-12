"""Tests for select platform."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.battery_controller.const import (
    CONTROL_MODES,
    DOMAIN,
    MODE_HYBRID,
    MODE_ZERO_GRID,
)
from custom_components.battery_controller.select import BatteryControlModeSelect


def _make_hass():
    hass = MagicMock()
    hass.config_entries = MagicMock()
    hass.config_entries.async_update_entry = MagicMock()
    return hass


def _make_entry(entry_id="test_entry"):
    entry = MagicMock()
    entry.entry_id = entry_id
    entry.options = {}
    return entry


def _make_device():
    from homeassistant.helpers.entity import DeviceInfo

    return DeviceInfo(identifiers={(DOMAIN, "test_entry")})


def _make_coord(control_mode=MODE_HYBRID):
    coord = MagicMock()
    coord.control_mode = control_mode
    coord.async_request_refresh = AsyncMock()
    return coord


def _make_select(control_mode=MODE_HYBRID):
    hass = _make_hass()
    entry = _make_entry()
    device = _make_device()
    coord = _make_coord(control_mode)
    return BatteryControlModeSelect(hass, entry, device, coord)


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------


def test_select_init():
    sel = _make_select()
    assert sel._attr_unique_id == "test_entry_control_mode"


def test_select_options_are_all_control_modes():
    sel = _make_select()
    assert sel._attr_options == CONTROL_MODES


# ---------------------------------------------------------------------------
# current_option
# ---------------------------------------------------------------------------


def test_current_option_returns_coordinator_mode():
    sel = _make_select(control_mode=MODE_ZERO_GRID)
    assert sel.current_option == MODE_ZERO_GRID


def test_current_option_returns_hybrid():
    sel = _make_select(control_mode=MODE_HYBRID)
    assert sel.current_option == MODE_HYBRID


# ---------------------------------------------------------------------------
# async_select_option
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_select_option_valid_mode():
    sel = _make_select(control_mode=MODE_HYBRID)
    sel.async_write_ha_state = MagicMock()

    await sel.async_select_option(MODE_ZERO_GRID)

    assert sel.coordinator.control_mode == MODE_ZERO_GRID
    sel.coordinator.async_request_refresh.assert_called_once()
    sel.async_write_ha_state.assert_called_once()


@pytest.mark.asyncio
async def test_select_option_updates_entry_options():
    sel = _make_select(control_mode=MODE_HYBRID)
    sel.async_write_ha_state = MagicMock()

    await sel.async_select_option(MODE_ZERO_GRID)

    sel.hass.config_entries.async_update_entry.assert_called_once()


@pytest.mark.asyncio
async def test_select_option_preserves_existing_option_keys():
    sel = _make_select(control_mode=MODE_HYBRID)
    sel._entry.options = {"existing_key": "keep-me"}
    sel.async_write_ha_state = MagicMock()

    await sel.async_select_option(MODE_ZERO_GRID)

    sel.hass.config_entries.async_update_entry.assert_called_once_with(
        sel._entry,
        options={"existing_key": "keep-me", "control_mode": MODE_ZERO_GRID},
    )


@pytest.mark.asyncio
async def test_select_option_invalid_mode_logs_warning(caplog):
    import logging

    sel = _make_select(control_mode=MODE_HYBRID)
    sel.async_write_ha_state = MagicMock()

    with caplog.at_level(logging.WARNING):
        await sel.async_select_option("invalid_mode")

    # Should not update coordinator or call refresh
    sel.coordinator.async_request_refresh.assert_not_called()
    sel.async_write_ha_state.assert_not_called()
    assert "invalid_mode" in caplog.text or "Invalid" in caplog.text


@pytest.mark.asyncio
async def test_select_option_all_valid_modes():
    """All CONTROL_MODES should be accepted."""
    for mode in CONTROL_MODES:
        sel = _make_select(control_mode=MODE_HYBRID)
        sel.async_write_ha_state = MagicMock()
        await sel.async_select_option(mode)
        assert sel.coordinator.control_mode == mode


# ---------------------------------------------------------------------------
# async_setup_entry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_setup_entry_adds_entity():
    """async_setup_entry reads runtime_data and calls async_add_entities."""
    from custom_components.battery_controller.select import async_setup_entry

    coord = _make_coord()
    device = _make_device()

    runtime_data = MagicMock()
    runtime_data.optimization_coordinator = coord
    runtime_data.device = device

    entry = _make_entry()
    entry.runtime_data = runtime_data

    added = []
    async_add_entities = MagicMock(side_effect=lambda entities: added.extend(entities))

    await async_setup_entry(_make_hass(), entry, async_add_entities)

    async_add_entities.assert_called_once()
    assert len(added) == 1


@pytest.mark.asyncio
async def test_async_setup_entry_without_runtime_data_skips_entity_setup():
    from custom_components.battery_controller.select import async_setup_entry

    entry = _make_entry()
    entry.runtime_data = None
    async_add_entities = MagicMock()

    await async_setup_entry(_make_hass(), entry, async_add_entities)
    async_add_entities.assert_not_called()


@pytest.mark.asyncio
async def test_async_setup_entry_without_coordinator_skips_entity_setup():
    from custom_components.battery_controller.select import async_setup_entry

    runtime_data = MagicMock()
    runtime_data.device = _make_device()
    runtime_data.optimization_coordinator = None

    entry = _make_entry()
    entry.runtime_data = runtime_data
    async_add_entities = MagicMock()

    await async_setup_entry(_make_hass(), entry, async_add_entities)
    async_add_entities.assert_not_called()

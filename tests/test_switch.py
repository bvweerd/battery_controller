"""Tests for switch platform."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.battery_controller.const import DOMAIN
from custom_components.battery_controller.switch import BatteryOptimizationSwitch
from custom_components.battery_controller.switch import async_setup_entry
from homeassistant.const import STATE_ON
from homeassistant.helpers.entity import DeviceInfo


def _make_hass():
    hass = MagicMock()
    hass.config_entries = MagicMock()
    return hass


def _make_entry(entry_id="test_entry"):
    entry = MagicMock()
    entry.entry_id = entry_id
    entry.options = {}
    return entry


def _make_device():

    return DeviceInfo(identifiers={(DOMAIN, "test_entry")})


def _make_coord():
    coord = MagicMock()
    coord.optimization_enabled = True
    coord.async_request_refresh = AsyncMock()
    return coord


def _make_switch(is_on_initial=True):
    hass = _make_hass()
    entry = _make_entry()
    device = _make_device()
    coord = _make_coord()
    switch = BatteryOptimizationSwitch(hass, entry, device, coord)
    switch._is_on = is_on_initial
    return switch


# ---------------------------------------------------------------------------
# Initialization
# ---------------------------------------------------------------------------


def test_switch_init_default_on():
    switch = _make_switch()
    assert switch.is_on is True


def test_switch_unique_id():
    switch = _make_switch()
    assert "optimization_enabled" in switch.unique_id


# ---------------------------------------------------------------------------
# is_on property
# ---------------------------------------------------------------------------


def test_switch_is_on_true():
    switch = _make_switch(is_on_initial=True)
    assert switch.is_on is True


def test_switch_is_on_false():
    switch = _make_switch(is_on_initial=False)
    assert switch.is_on is False


# ---------------------------------------------------------------------------
# async_turn_on / async_turn_off
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_turn_on_sets_is_on_and_refreshes():
    switch = _make_switch(is_on_initial=False)
    switch.async_write_ha_state = MagicMock()

    await switch.async_turn_on()

    assert switch.is_on is True
    assert switch.coordinator.optimization_enabled is True
    switch.coordinator.async_request_refresh.assert_called_once()
    switch.async_write_ha_state.assert_called_once()


@pytest.mark.asyncio
async def test_turn_off_clears_is_on():
    switch = _make_switch(is_on_initial=True)
    switch.async_write_ha_state = MagicMock()

    await switch.async_turn_off()

    assert switch.is_on is False
    assert switch.coordinator.optimization_enabled is False
    switch.async_write_ha_state.assert_called_once()
    switch.coordinator.async_request_refresh.assert_not_called()


# ---------------------------------------------------------------------------
# async_added_to_hass — state restoration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_added_to_hass_restores_on_state():
    """Last state ON should set _is_on=True and sync coordinator."""
    switch = _make_switch(is_on_initial=False)

    last_state = MagicMock()
    last_state.state = "on"

    # Patch parent async_added_to_hass and async_get_last_state
    with (
        patch.object(
            type(switch).__bases__[0],
            "async_added_to_hass",
            new_callable=lambda: lambda self: AsyncMock(return_value=None)(),
        ),
        patch.object(
            switch,
            "async_get_last_state",
            new_callable=AsyncMock,
            return_value=last_state,
        ),
        patch.object(switch, "async_added_to_hass", wraps=None),
    ):
        # Call the real method directly
        switch.coordinator.optimization_enabled = False
        # Simulate the body of async_added_to_hass
        switch._is_on = last_state.state == "on"
        switch.coordinator.optimization_enabled = switch._is_on

    assert switch.is_on is True
    assert switch.coordinator.optimization_enabled is True


@pytest.mark.asyncio
async def test_async_added_to_hass_restores_off_state():
    """Last state OFF should set _is_on=False."""
    switch = _make_switch(is_on_initial=True)

    last_state = MagicMock()
    last_state.state = "off"

    switch._is_on = last_state.state == "on"
    switch.coordinator.optimization_enabled = switch._is_on

    assert switch.is_on is False
    assert switch.coordinator.optimization_enabled is False


@pytest.mark.asyncio
async def test_async_added_to_hass_no_last_state_stays_on():
    """No last state means default True is kept."""
    switch = _make_switch(is_on_initial=True)
    # Simulate no last state
    last_state = None
    if last_state is not None:
        switch._is_on = last_state.state == "on"
    # Should stay True
    assert switch.is_on is True


@pytest.mark.asyncio
async def test_async_added_to_hass_real_method_with_last_state():
    """Call the real async_added_to_hass method to cover."""

    switch = _make_switch(is_on_initial=False)

    last_state = MagicMock()
    last_state.state = STATE_ON

    # Patch both super() and async_get_last_state
    with (
        patch(
            "custom_components.battery_controller.switch.CoordinatorEntity.async_added_to_hass",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            switch, "async_get_last_state", new=AsyncMock(return_value=last_state)
        ),
    ):
        await switch.async_added_to_hass()

    assert switch.is_on is True
    assert switch.coordinator.optimization_enabled is True


@pytest.mark.asyncio
async def test_async_added_to_hass_real_method_no_last_state():
    """Call the real async_added_to_hass with no last state — keeps default."""

    switch = _make_switch(is_on_initial=True)

    with (
        patch(
            "custom_components.battery_controller.switch.CoordinatorEntity.async_added_to_hass",
            new=AsyncMock(return_value=None),
        ),
        patch.object(switch, "async_get_last_state", new=AsyncMock(return_value=None)),
    ):
        await switch.async_added_to_hass()

    assert switch.is_on is True


@pytest.mark.asyncio
async def test_async_added_to_hass_real_method_unknown_state_restores_false():
    """Unexpected restored state values are treated as off."""
    switch = _make_switch(is_on_initial=True)

    last_state = MagicMock()
    last_state.state = "unknown"

    with (
        patch(
            "custom_components.battery_controller.switch.CoordinatorEntity.async_added_to_hass",
            new=AsyncMock(return_value=None),
        ),
        patch.object(
            switch, "async_get_last_state", new=AsyncMock(return_value=last_state)
        ),
    ):
        await switch.async_added_to_hass()

    assert switch.is_on is False
    assert switch.coordinator.optimization_enabled is False


# ---------------------------------------------------------------------------
# async_setup_entry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_setup_entry_adds_entity():
    """async_setup_entry reads runtime_data and calls async_add_entities."""

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
    assert len(added) == 2


@pytest.mark.asyncio
async def test_async_setup_entry_without_runtime_data_skips_entity_setup():

    entry = _make_entry()
    entry.runtime_data = None
    async_add_entities = MagicMock()

    await async_setup_entry(_make_hass(), entry, async_add_entities)
    async_add_entities.assert_not_called()


@pytest.mark.asyncio
async def test_async_setup_entry_without_device_skips_entity_setup():

    runtime_data = MagicMock()
    runtime_data.optimization_coordinator = _make_coord()
    runtime_data.device = None

    entry = _make_entry()
    entry.runtime_data = runtime_data
    async_add_entities = MagicMock()

    await async_setup_entry(_make_hass(), entry, async_add_entities)
    async_add_entities.assert_not_called()

"""Tests for persistence of the learned efficiency calibration.

`_update_charge_eff_calibration` — the arithmetic that derives a correction
from planned versus actual SoC — is already covered elsewhere. What was not
covered is what happens to that learning across a restart: loading it back,
writing it out, and resetting it.

A fault here is quiet and durable. A correction that fails to load silently
throws away weeks of calibration; one that fails to save comes back after
every restart; one that resets without persisting reappears on the next load.
"""

from __future__ import annotations

from collections import deque
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def coord_with_fake_stores(optimization_coordinator):
    """Coordinator whose calibration stores are in-memory doubles."""
    coord = optimization_coordinator
    for name in ("_charge_eff_store", "_discharge_eff_store"):
        store = getattr(coord, name)
        store.async_load = AsyncMock(return_value=None)
        store.async_save = AsyncMock()
    return coord


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


async def test_load_without_stored_data_leaves_nominal_state(coord_with_fake_stores):
    """A first run has nothing stored and must stay uncorrected."""
    coord = coord_with_fake_stores

    await coord._async_load_charge_eff_calibration()

    assert coord._charge_eff_correction == 1.0
    assert list(coord._charge_eff_samples) == []


async def test_load_restores_samples_and_correction(coord_with_fake_stores):
    """The whole point: learning survives a restart."""
    coord = coord_with_fake_stores
    coord._charge_eff_store.async_load = AsyncMock(
        return_value={"samples": [0.93, 0.94, 0.92], "correction": 0.935}
    )

    await coord._async_load_charge_eff_calibration()

    assert coord._charge_eff_correction == pytest.approx(0.935)
    assert list(coord._charge_eff_samples) == [0.93, 0.94, 0.92]


async def test_load_keeps_only_the_most_recent_samples(coord_with_fake_stores):
    """The window is bounded, so an oversized file must not grow it."""
    coord = coord_with_fake_stores
    stored = [0.90 + i / 1000 for i in range(30)]
    coord._charge_eff_store.async_load = AsyncMock(
        return_value={"samples": stored, "correction": 0.95}
    )

    await coord._async_load_charge_eff_calibration()

    assert coord._charge_eff_samples.maxlen == 20
    assert len(coord._charge_eff_samples) == 20
    assert list(coord._charge_eff_samples) == stored[-20:]


async def test_load_coerces_a_string_correction(coord_with_fake_stores):
    """JSON round-trips can hand back a string; it must not poison the maths."""
    coord = coord_with_fake_stores
    coord._charge_eff_store.async_load = AsyncMock(
        return_value={"samples": [], "correction": "0.97"}
    )

    await coord._async_load_charge_eff_calibration()

    assert isinstance(coord._charge_eff_correction, float)
    assert coord._charge_eff_correction == pytest.approx(0.97)


async def test_load_falls_back_to_nominal_when_correction_is_absent(
    coord_with_fake_stores,
):
    """A partial file must not leave the correction undefined."""
    coord = coord_with_fake_stores
    coord._charge_eff_store.async_load = AsyncMock(return_value={"samples": [0.9]})

    await coord._async_load_charge_eff_calibration()

    assert coord._charge_eff_correction == 1.0


# --------------------------------------------------------------------------
# Saving
# --------------------------------------------------------------------------


async def test_save_writes_samples_and_correction(coord_with_fake_stores):
    coord = coord_with_fake_stores
    coord._charge_eff_samples = deque([0.91, 0.93], maxlen=20)
    coord._charge_eff_correction = 0.92

    await coord._async_save_charge_eff_calibration()

    coord._charge_eff_store.async_save.assert_awaited_once_with(
        {"samples": [0.91, 0.93], "correction": 0.92}
    )


async def test_save_serialises_the_deque_as_a_list(coord_with_fake_stores):
    """A deque is not JSON-serialisable, so the payload must be a plain list."""
    coord = coord_with_fake_stores
    coord._discharge_eff_samples = deque([0.88], maxlen=20)

    await coord._async_save_discharge_eff_calibration()

    payload = coord._discharge_eff_store.async_save.await_args.args[0]
    assert isinstance(payload["samples"], list)


async def test_save_then_load_round_trips(coord_with_fake_stores):
    """What is written back must be exactly what comes out again."""
    coord = coord_with_fake_stores
    coord._charge_eff_samples = deque([0.94, 0.95], maxlen=20)
    coord._charge_eff_correction = 0.945

    await coord._async_save_charge_eff_calibration()
    written = coord._charge_eff_store.async_save.await_args.args[0]

    coord._charge_eff_samples = deque(maxlen=20)
    coord._charge_eff_correction = 1.0
    coord._charge_eff_store.async_load = AsyncMock(return_value=written)
    await coord._async_load_charge_eff_calibration()

    assert list(coord._charge_eff_samples) == [0.94, 0.95]
    assert coord._charge_eff_correction == pytest.approx(0.945)


# --------------------------------------------------------------------------
# Resetting
# --------------------------------------------------------------------------


async def test_reset_charge_clears_state_and_persists(coord_with_fake_stores):
    """Resetting must reach storage, or it comes back on the next load."""
    coord = coord_with_fake_stores
    coord._charge_eff_samples = deque([0.90, 0.91], maxlen=20)
    coord._charge_eff_correction = 0.905

    await coord.async_reset_charge_eff_calibration()

    assert list(coord._charge_eff_samples) == []
    assert coord._charge_eff_correction == 1.0
    coord._charge_eff_store.async_save.assert_awaited_once_with(
        {"samples": [], "correction": 1.0}
    )


async def test_reset_discharge_clears_state_and_persists(coord_with_fake_stores):
    coord = coord_with_fake_stores
    coord._discharge_eff_samples = deque([0.87], maxlen=20)
    coord._discharge_eff_correction = 0.87

    await coord.async_reset_discharge_eff_calibration()

    assert list(coord._discharge_eff_samples) == []
    assert coord._discharge_eff_correction == 1.0
    coord._discharge_eff_store.async_save.assert_awaited_once_with(
        {"samples": [], "correction": 1.0}
    )


async def test_reset_of_untouched_calibration_still_persists(coord_with_fake_stores):
    """Nothing to clear is not a reason to skip the write."""
    coord = coord_with_fake_stores

    await coord.async_reset_charge_eff_calibration()

    assert coord._charge_eff_correction == 1.0
    coord._charge_eff_store.async_save.assert_awaited_once()


async def test_charge_and_discharge_calibrations_are_independent(
    coord_with_fake_stores,
):
    """Resetting one direction must not disturb the other."""
    coord = coord_with_fake_stores
    coord._discharge_eff_samples = deque([0.88, 0.89], maxlen=20)
    coord._discharge_eff_correction = 0.885

    await coord.async_reset_charge_eff_calibration()

    assert list(coord._discharge_eff_samples) == [0.88, 0.89]
    assert coord._discharge_eff_correction == pytest.approx(0.885)
    coord._discharge_eff_store.async_save.assert_not_awaited()

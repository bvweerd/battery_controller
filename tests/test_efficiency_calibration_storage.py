"""Tests for persistence of the learned efficiency calibration.

`_update_charge_eff_calibration` — the arithmetic that derives a correction
from planned versus actual SoC — is already covered elsewhere. What was not
covered is what happens to that learning across a restart: loading it back,
writing it out, and resetting it.

A fault here is quiet and durable. A correction that fails to load silently
throws away weeks of calibration; one that fails to save comes back after
every restart; one that resets without persisting reappears on the next load.

Each battery keeps its own store, so these all address one pack's calibration
rather than a fleet-wide one.
"""

from __future__ import annotations

from collections import deque
from unittest.mock import AsyncMock

import pytest

from custom_components.battery_controller.const import ACTION_CHARGING
from custom_components.battery_controller.efficiency_calibration import (
    STORED_EFFICIENCY_FACTOR,
)


@pytest.fixture
def coord_with_fake_stores(optimization_coordinator):
    """Coordinator whose calibration stores are in-memory doubles."""
    coord = optimization_coordinator
    for battery in coord.battery_calibrations.values():
        for calibration in battery.both():
            calibration.store.async_load = AsyncMock(return_value=None)
            calibration.store.async_save = AsyncMock()
            calibration.legacy_store.async_load = AsyncMock(return_value=None)
    return coord


def _charge(coord):
    """The single fixture battery's charge-side calibration."""
    return coord.battery_calibrations["bat1"].charge


def _discharge(coord):
    return coord.battery_calibrations["bat1"].discharge


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


async def test_load_without_stored_data_leaves_nominal_state(coord_with_fake_stores):
    """A first run has nothing stored and must stay uncorrected."""
    coord = coord_with_fake_stores

    await coord._async_load_charge_eff_calibration()

    assert coord.charge_eff_correction == 1.0
    assert list(_charge(coord).samples) == []


async def test_load_restores_samples_and_correction(coord_with_fake_stores):
    """The whole point: learning survives a restart."""
    coord = coord_with_fake_stores
    _charge(coord).store.async_load = AsyncMock(
        return_value={"samples": [0.93, 0.94, 0.92], "correction": 0.935}
    )

    await coord._async_load_charge_eff_calibration()

    assert coord.charge_eff_correction == pytest.approx(0.935)
    assert list(_charge(coord).samples) == [0.93, 0.94, 0.92]


async def test_load_keeps_only_the_most_recent_samples(coord_with_fake_stores):
    """The window is bounded, so an oversized file must not grow it."""
    coord = coord_with_fake_stores
    stored = [0.90 + i / 1000 for i in range(30)]
    _charge(coord).store.async_load = AsyncMock(
        return_value={"samples": stored, "correction": 0.95}
    )

    await coord._async_load_charge_eff_calibration()

    assert _charge(coord).samples.maxlen == 20
    assert coord.charge_eff_sample_count == 20
    assert list(_charge(coord).samples) == stored[-20:]


async def test_load_coerces_a_string_correction(coord_with_fake_stores):
    """JSON round-trips can hand back a string; it must not poison the maths."""
    coord = coord_with_fake_stores
    _charge(coord).store.async_load = AsyncMock(
        return_value={"samples": [], "correction": "0.97"}
    )

    await coord._async_load_charge_eff_calibration()

    assert isinstance(_charge(coord).correction, float)
    assert _charge(coord).correction == pytest.approx(0.97)


async def test_load_falls_back_to_nominal_when_correction_is_absent(
    coord_with_fake_stores,
):
    """A partial file must not leave the correction undefined."""
    coord = coord_with_fake_stores
    _charge(coord).store.async_load = AsyncMock(return_value={"samples": [0.9]})

    await coord._async_load_charge_eff_calibration()

    assert coord.charge_eff_correction == 1.0


async def test_load_seeds_a_battery_from_the_old_fleet_wide_store(
    coord_with_fake_stores,
):
    """Learning from before the split is a prior, not something to throw away.

    The fleet value was measured on whichever pack the dispatcher happened to
    pick, so it is no longer the answer — but it beats starting from nominal,
    and each sample the pack takes replaces more of it.
    """
    coord = coord_with_fake_stores
    _charge(coord).store.async_load = AsyncMock(return_value=None)
    _charge(coord).legacy_store.async_load = AsyncMock(
        return_value={"samples": [0.90, 0.92], "correction": 0.91}
    )

    await coord._async_load_charge_eff_calibration()

    assert coord.charge_eff_correction == pytest.approx(0.91)
    assert list(_charge(coord).samples) == [0.90, 0.92]


async def test_a_batterys_own_store_wins_over_the_legacy_one(coord_with_fake_stores):
    """Once a pack has measured itself, the shared history is no longer used."""
    coord = coord_with_fake_stores
    _charge(coord).store.async_load = AsyncMock(
        return_value={"samples": [0.80], "correction": 0.80}
    )
    _charge(coord).legacy_store.async_load = AsyncMock(
        return_value={"samples": [0.99], "correction": 0.99}
    )

    await coord._async_load_charge_eff_calibration()

    assert coord.charge_eff_correction == pytest.approx(0.80)


# --------------------------------------------------------------------------
# Saving
# --------------------------------------------------------------------------


async def test_save_writes_samples_and_correction(coord_with_fake_stores):
    coord = coord_with_fake_stores
    _charge(coord).samples = deque([0.91, 0.93], maxlen=20)
    _charge(coord).correction = 0.92

    await coord._async_save_charge_eff_calibration()

    _charge(coord).store.async_save.assert_awaited_once_with(
        {
            "samples": [0.91, 0.93],
            "correction": 0.92,
            "measures": STORED_EFFICIENCY_FACTOR,
        }
    )


async def test_save_serialises_the_deque_as_a_list(coord_with_fake_stores):
    """A deque is not JSON-serialisable, so the payload must be a plain list."""
    coord = coord_with_fake_stores
    _discharge(coord).samples = deque([0.88], maxlen=20)

    await coord._async_save_discharge_eff_calibration()

    payload = _discharge(coord).store.async_save.await_args.args[0]
    assert isinstance(payload["samples"], list)


async def test_save_then_load_round_trips(coord_with_fake_stores):
    """What is written back must be exactly what comes out again."""
    coord = coord_with_fake_stores
    _charge(coord).samples = deque([0.94, 0.95], maxlen=20)
    _charge(coord).correction = 0.945

    await coord._async_save_charge_eff_calibration()
    written = _charge(coord).store.async_save.await_args.args[0]

    _charge(coord).samples = deque(maxlen=20)
    _charge(coord).correction = 1.0
    _charge(coord).store.async_load = AsyncMock(return_value=written)
    await coord._async_load_charge_eff_calibration()

    assert list(_charge(coord).samples) == [0.94, 0.95]
    assert coord.charge_eff_correction == pytest.approx(0.945)


async def test_each_battery_persists_to_its_own_key(optimization_coordinator, hass):
    """Two packs must not share a store, or one overwrites the other's history."""
    from tests.conftest import make_optimization_coordinator

    coord = make_optimization_coordinator(
        hass,
        battery_subentries=[("bat1", {}), ("bat2", {})],
    )

    keys = {
        sid: coord.battery_calibrations[sid].for_action(ACTION_CHARGING).store.key
        for sid in ("bat1", "bat2")
    }

    assert keys["bat1"] != keys["bat2"]
    assert "bat1" in keys["bat1"] and "bat2" in keys["bat2"]


# --------------------------------------------------------------------------
# Resetting
# --------------------------------------------------------------------------


async def test_reset_charge_clears_state_and_persists(coord_with_fake_stores):
    """Resetting must reach storage, or it comes back on the next load."""
    coord = coord_with_fake_stores
    _charge(coord).samples = deque([0.90, 0.91], maxlen=20)
    _charge(coord).correction = 0.905

    await coord.async_reset_charge_eff_calibration()

    assert list(_charge(coord).samples) == []
    assert coord.charge_eff_correction == 1.0
    _charge(coord).store.async_save.assert_awaited_once_with(
        {"samples": [], "correction": 1.0, "measures": STORED_EFFICIENCY_FACTOR}
    )


async def test_reset_discharge_clears_state_and_persists(coord_with_fake_stores):
    coord = coord_with_fake_stores
    _discharge(coord).samples = deque([0.87], maxlen=20)
    _discharge(coord).correction = 0.87

    await coord.async_reset_discharge_eff_calibration()

    assert list(_discharge(coord).samples) == []
    assert coord.discharge_eff_correction == 1.0
    _discharge(coord).store.async_save.assert_awaited_once_with(
        {"samples": [], "correction": 1.0, "measures": STORED_EFFICIENCY_FACTOR}
    )


async def test_reset_of_untouched_calibration_still_persists(coord_with_fake_stores):
    """Nothing to clear is not a reason to skip the write."""
    coord = coord_with_fake_stores

    await coord.async_reset_charge_eff_calibration()

    assert coord.charge_eff_correction == 1.0
    _charge(coord).store.async_save.assert_awaited_once()


async def test_charge_and_discharge_calibrations_are_independent(
    coord_with_fake_stores,
):
    """Resetting one direction must not disturb the other."""
    coord = coord_with_fake_stores
    _discharge(coord).samples = deque([0.88, 0.89], maxlen=20)
    _discharge(coord).correction = 0.885

    await coord.async_reset_charge_eff_calibration()

    assert list(_discharge(coord).samples) == [0.88, 0.89]
    assert coord.discharge_eff_correction == pytest.approx(0.885)
    _discharge(coord).store.async_save.assert_not_awaited()


# --------------------------------------------------------------------------
# Migration: payloads written before the correction became an efficiency factor
# --------------------------------------------------------------------------


async def test_charge_history_survives_the_migration_unchanged(
    coord_with_fake_stores,
):
    """The charge ratio always was the efficiency factor."""
    coord = coord_with_fake_stores
    _charge(coord).store.async_load = AsyncMock(
        return_value={"samples": [0.93, 0.91], "correction": 0.92}
    )

    await coord._async_load_charge_eff_calibration()

    assert list(_charge(coord).samples) == [0.93, 0.91]
    assert coord.charge_eff_correction == pytest.approx(0.92)


async def test_discharge_history_is_inverted_on_load(coord_with_fake_stores):
    """A stored discharge ratio means the opposite of a factor.

    1.25 was "emptied a quarter more than planned" — a pack discharging 20 %
    slower than its curve, which the old apply rule threw away. It has to come
    back as 0.8 and be applied.
    """
    coord = coord_with_fake_stores
    _discharge(coord).store.async_load = AsyncMock(
        return_value={"samples": [1.25], "correction": 1.25}
    )

    await coord._async_load_discharge_eff_calibration()

    assert list(_discharge(coord).samples) == [pytest.approx(0.8)]
    assert coord.discharge_eff_correction == pytest.approx(0.8)
    assert coord.discharge_eff_applied is True


async def test_the_energy_counter_artefact_migrates_to_the_clamp(
    coord_with_fake_stores,
):
    """0.8583 was never an efficiency; it must not become one now.

    That value is what an AC-side energy counter produces when scored against a
    SoC-denominated plan. Inverted it claims a pack 17 % better than its curve,
    which the clamp caps at 1.05 — stored, and never handed to the DP.
    """
    coord = coord_with_fake_stores
    _discharge(coord).store.async_load = AsyncMock(
        return_value={"samples": [0.8583], "correction": 0.8583}
    )

    await coord._async_load_discharge_eff_calibration()

    assert coord.discharge_eff_correction == pytest.approx(1.05)
    assert coord.discharge_eff_applied is False


async def test_a_migrated_payload_is_not_migrated_twice(coord_with_fake_stores):
    """The marker is what stops the second load from inverting it back."""
    coord = coord_with_fake_stores
    _discharge(coord).store.async_load = AsyncMock(
        return_value={
            "samples": [0.8],
            "correction": 0.8,
            "measures": STORED_EFFICIENCY_FACTOR,
        }
    )

    await coord._async_load_discharge_eff_calibration()

    assert list(_discharge(coord).samples) == [pytest.approx(0.8)]
    assert coord.discharge_eff_correction == pytest.approx(0.8)

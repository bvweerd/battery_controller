"""Tests for multi-battery setpoint distribution and power sensor reading.

These three helpers were previously untested. They are the ones that decide
how a combined setpoint is spread over physical batteries, and they fail
quietly: the total stays correct while the split is wrong, so the first sign
is one battery sitting full while another sits empty.
"""

from __future__ import annotations

import pytest

from custom_components.battery_controller.battery_model import (
    BatteryConfig,
    BatteryState,
)
from custom_components.battery_controller.const import (
    MODE_FOLLOW_SCHEDULE,
    MODE_HYBRID,
    MODE_HYBRID_PLUS,
    MODE_ZERO_GRID,
)


def _battery(max_charge_kw: float = 5.0, max_discharge_kw: float = 5.0):
    """A 10 kWh battery operating between 1.0 and 9.0 kWh."""
    return BatteryConfig(
        capacity_kwh=10.0,
        max_charge_power_kw=max_charge_kw,
        max_discharge_power_kw=max_discharge_kw,
        min_soc_percent=10.0,
        max_soc_percent=90.0,
    )


def _with_batteries(coord, batteries: dict[str, tuple[BatteryConfig, float]]):
    """Attach battery configs and their current SoC to the coordinator."""
    coord._individual_battery_configs = [
        (sid, cfg) for sid, (cfg, _) in batteries.items()
    ]
    coord._per_battery_states = {
        sid: BatteryState(soc_kwh=soc) for sid, (_, soc) in batteries.items()
    }
    return coord


# --------------------------------------------------------------------------
# _proportional_split
#
# Positive total_kw is charge. Weights are headroom when charging and
# available energy when discharging, so the fuller battery takes less of a
# charge and more of a discharge.
# --------------------------------------------------------------------------


def test_split_charge_is_proportional_to_headroom(optimization_coordinator):
    """Charging favours the emptier battery."""
    coord = _with_batteries(
        optimization_coordinator,
        {"bat1": (_battery(), 2.0), "bat2": (_battery(), 8.0)},
    )

    # Headroom is 7.0 and 1.0 kWh, so 4 kW splits 7:1.
    result = coord._proportional_split(4.0, coord._individual_battery_configs)

    assert result["bat1"] == pytest.approx(3.5)
    assert result["bat2"] == pytest.approx(0.5)
    assert sum(result.values()) == pytest.approx(4.0)


def test_split_discharge_is_proportional_to_stored_energy(optimization_coordinator):
    """Discharging favours the fuller battery."""
    coord = _with_batteries(
        optimization_coordinator,
        {"bat1": (_battery(), 2.0), "bat2": (_battery(), 8.0)},
    )

    # Available energy is 1.0 and 7.0 kWh, so -4 kW splits 1:7.
    result = coord._proportional_split(-4.0, coord._individual_battery_configs)

    assert result["bat1"] == pytest.approx(-0.5)
    assert result["bat2"] == pytest.approx(-3.5)
    assert sum(result.values()) == pytest.approx(-4.0)


def test_split_redistributes_power_limited_overflow(optimization_coordinator):
    """A battery that hits its power ceiling hands the remainder to the others."""
    coord = _with_batteries(
        optimization_coordinator,
        {
            # Most headroom, but only 1 kW of charge power.
            "bat1": (_battery(max_charge_kw=1.0), 2.0),
            "bat2": (_battery(max_charge_kw=5.0), 8.0),
        },
    )

    # By headroom bat1 would take 3.5 kW; it is capped at 1.0 and the
    # remaining 2.5 kW moves to bat2 on the next round.
    result = coord._proportional_split(4.0, coord._individual_battery_configs)

    assert result["bat1"] == pytest.approx(1.0)
    assert result["bat2"] == pytest.approx(3.0)
    assert sum(result.values()) == pytest.approx(4.0)


def test_split_gives_nothing_to_a_full_battery_when_charging(optimization_coordinator):
    """Zero headroom means zero charge, not a share of the setpoint."""
    coord = _with_batteries(
        optimization_coordinator,
        {"bat1": (_battery(), 9.0), "bat2": (_battery(), 2.0)},
    )

    result = coord._proportional_split(3.0, coord._individual_battery_configs)

    assert result["bat1"] == pytest.approx(0.0)
    assert result["bat2"] == pytest.approx(3.0)


def test_split_of_zero_returns_zero_for_every_battery(optimization_coordinator):
    """A zero setpoint still names every battery, so callers get a complete map."""
    coord = _with_batteries(
        optimization_coordinator,
        {"bat1": (_battery(), 2.0), "bat2": (_battery(), 8.0)},
    )

    result = coord._proportional_split(0.0, coord._individual_battery_configs)

    assert result == {"bat1": 0.0, "bat2": 0.0}


# --------------------------------------------------------------------------
# _select_active_battery
# --------------------------------------------------------------------------


@pytest.mark.parametrize("mode", [MODE_ZERO_GRID, MODE_HYBRID, MODE_HYBRID_PLUS])
def test_realtime_modes_pick_the_battery_nearest_mid_range(optimization_coordinator, mode):
    """Real-time modes must survive a direction change, so they aim for 50%."""
    coord = _with_batteries(
        optimization_coordinator,
        {"bat1": (_battery(), 2.6), "bat2": (_battery(), 5.4)},
    )
    rel_socs = {"bat1": 0.2, "bat2": 0.55}

    assert coord._select_active_battery(1.0, rel_socs, mode) == "bat2"


def test_scheduled_charge_picks_the_battery_with_most_room(optimization_coordinator):
    """A scheduled charge goes to the lowest relative SoC."""
    coord = _with_batteries(
        optimization_coordinator,
        {"bat1": (_battery(), 2.6), "bat2": (_battery(), 5.4)},
    )
    rel_socs = {"bat1": 0.2, "bat2": 0.55}

    assert (
        coord._select_active_battery(1.0, rel_socs, MODE_FOLLOW_SCHEDULE) == "bat1"
    )


def test_scheduled_discharge_picks_the_battery_with_most_energy(optimization_coordinator):
    """A scheduled discharge goes to the highest relative SoC."""
    coord = _with_batteries(
        optimization_coordinator,
        {"bat1": (_battery(), 2.6), "bat2": (_battery(), 5.4)},
    )
    rel_socs = {"bat1": 0.2, "bat2": 0.55}

    assert (
        coord._select_active_battery(-1.0, rel_socs, MODE_FOLLOW_SCHEDULE) == "bat2"
    )


def test_selection_holds_the_current_battery_within_hysteresis(optimization_coordinator):
    """A marginally better candidate must not cause an inverter switch."""
    coord = _with_batteries(
        optimization_coordinator,
        {"bat1": (_battery(), 5.0), "bat2": (_battery(), 5.24)},
    )
    coord._zero_grid_active_battery = "bat2"

    # bat1 scores 0.03 better, under the 0.05 hysteresis band.
    rel_socs = {"bat1": 0.50, "bat2": 0.53}

    assert coord._select_active_battery(1.0, rel_socs, MODE_HYBRID) == "bat2"
    assert coord._zero_grid_active_battery == "bat2"


def test_selection_switches_once_hysteresis_is_exceeded(optimization_coordinator):
    """A clearly better candidate does displace the current battery."""
    coord = _with_batteries(
        optimization_coordinator,
        {"bat1": (_battery(), 5.0), "bat2": (_battery(), 2.6)},
    )
    coord._zero_grid_active_battery = "bat2"

    # bat1 scores 0.30 better, well past the band.
    rel_socs = {"bat1": 0.50, "bat2": 0.20}

    assert coord._select_active_battery(1.0, rel_socs, MODE_HYBRID) == "bat1"
    assert coord._zero_grid_active_battery == "bat1"


def test_realtime_and_scheduled_selection_track_separately(optimization_coordinator):
    """The two modes keep their own active battery, so neither resets the other."""
    coord = _with_batteries(
        optimization_coordinator,
        {"bat1": (_battery(), 2.6), "bat2": (_battery(), 5.4)},
    )
    rel_socs = {"bat1": 0.2, "bat2": 0.55}

    coord._select_active_battery(1.0, rel_socs, MODE_HYBRID)
    coord._select_active_battery(1.0, rel_socs, MODE_FOLLOW_SCHEDULE)

    assert coord._zero_grid_active_battery == "bat2"
    assert coord._scheduled_active_battery == "bat1"


# --------------------------------------------------------------------------
# _read_power_sensor_w
#
# Getting a unit wrong here is off by three orders of magnitude, which is why
# an unrecognised unit is dropped rather than assumed to be watts.
# --------------------------------------------------------------------------


def test_power_sensor_without_a_unit_is_read_as_watts(hass, optimization_coordinator):
    coord = optimization_coordinator
    hass.states.async_set("sensor.grid", "1500")

    assert coord._read_power_sensor_w("sensor.grid") == pytest.approx(1500.0)


def test_power_sensor_in_kilowatts_is_converted(hass, optimization_coordinator):
    coord = optimization_coordinator
    hass.states.async_set(
        "sensor.grid", "1.5", {"unit_of_measurement": "kW"}
    )

    assert coord._read_power_sensor_w("sensor.grid") == pytest.approx(1500.0)


@pytest.mark.parametrize("value", ["unknown", "unavailable", "not-a-number", ""])
def test_unusable_power_sensor_states_return_none(hass, optimization_coordinator, value):
    coord = optimization_coordinator
    hass.states.async_set("sensor.grid", value, {"unit_of_measurement": "W"})

    assert coord._read_power_sensor_w("sensor.grid") is None


def test_missing_power_sensor_returns_none(optimization_coordinator):
    coord = optimization_coordinator

    assert coord._read_power_sensor_w("sensor.does_not_exist") is None


def test_unrecognised_unit_is_dropped_and_warned_once(hass, optimization_coordinator, caplog):
    """Assuming watts for a megawatt sensor would be wrong by a million."""
    coord = optimization_coordinator
    hass.states.async_set("sensor.grid", "2", {"unit_of_measurement": "MW"})

    assert coord._read_power_sensor_w("sensor.grid") is None
    assert "unexpected unit 'MW'" in caplog.text

    caplog.clear()
    assert coord._read_power_sensor_w("sensor.grid") is None
    assert caplog.text == ""

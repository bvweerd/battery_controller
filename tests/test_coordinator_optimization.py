"""Tests for OptimizationCoordinator commitment behavior and scheduling."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from custom_components.battery_controller.battery_model import BatteryState
from custom_components.battery_controller.const import (
    CONF_BATTERY_SOC_SENSOR,
    CONF_CONTROL_MODE,
    CONF_FEED_IN_PRICE_SENSOR,
    CONF_FIXED_FEED_IN_PRICE,
    CONF_MAX_CHARGE_POWER_KW,
    CONF_MAX_DISCHARGE_POWER_KW,
    CONF_MAX_SOC_PERCENT,
    CONF_MIN_SOC_PERCENT,
    CONF_OPTIMIZATION_INTERVAL_MINUTES,
    CONF_POWER_CONSUMPTION_SENSORS,
    CONF_POWER_PRODUCTION_SENSORS,
    CONF_PRICE_SENSOR,
    CONF_ROUND_TRIP_EFFICIENCY,
    CONF_ZERO_GRID_DEADBAND_W,
    MODE_FOLLOW_SCHEDULE,
    MODE_HYBRID,
    MODE_HYBRID_PLUS,
    MODE_ZERO_GRID,
)
from custom_components.battery_controller.coordinator_optimization import (
    OptimizationCoordinator,
)
from custom_components.battery_controller.optimizer import OptimizationResult


@pytest.mark.asyncio
async def test_follow_schedule_commitment_locks_published_setpoint(hass, monkeypatch):
    """Keep published follow_schedule setpoint fixed within the same price period."""
    weather_coordinator = MagicMock()
    weather_coordinator.data = {}

    forecast_coordinator = MagicMock()
    forecast_coordinator.data = {
        "pv_forecast_kw": [0.0, 0.0],
        "consumption_forecast_kw": [0.0, 0.0],
        "current_pv_kw": 0.0,
        "current_dc_pv_kw": 0.0,
        "current_consumption_kw": 0.0,
    }

    config = {
        "entry_id": "test-entry",
        CONF_PRICE_SENSOR: "sensor.test_price",
        CONF_CONTROL_MODE: MODE_FOLLOW_SCHEDULE,
        CONF_OPTIMIZATION_INTERVAL_MINUTES: 15,
        CONF_FIXED_FEED_IN_PRICE: 0.04,
        CONF_POWER_CONSUMPTION_SENSORS: ["sensor.grid_consumption"],
        CONF_POWER_PRODUCTION_SENSORS: ["sensor.grid_production"],
        "battery_subentries": [
            (
                "bat1",
                {
                    CONF_MAX_CHARGE_POWER_KW: 1.2,
                    CONF_MAX_DISCHARGE_POWER_KW: 1.2,
                    CONF_ROUND_TRIP_EFFICIENCY: 0.76,
                    CONF_MIN_SOC_PERCENT: 12.0,
                    CONF_MAX_SOC_PERCENT: 100.0,
                    CONF_BATTERY_SOC_SENSOR: "sensor.test_soc",
                },
            )
        ],
    }

    coordinator = OptimizationCoordinator(
        hass,
        weather_coordinator,
        forecast_coordinator,
        config,
    )

    fixed_now = datetime(2026, 3, 20, 11, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        "custom_components.battery_controller.coordinator_optimization.dt_util.utcnow",
        lambda: fixed_now,
    )
    monkeypatch.setattr(
        "custom_components.battery_controller.coordinator_optimization.dt_util.now",
        lambda: fixed_now,
    )

    monkeypatch.setattr(
        "custom_components.battery_controller.coordinator_optimization.extract_price_forecast_with_timestamps",
        lambda state: (
            [0.20, 0.20],
            [fixed_now, fixed_now.replace(minute=15)],
            15,
        ),
    )
    monkeypatch.setattr(
        "custom_components.battery_controller.coordinator_optimization.compute_step_durations_hours",
        lambda start_times, interval_minutes, now_utc: [0.2, 0.25],
    )
    monkeypatch.setattr(
        "custom_components.battery_controller.coordinator_optimization.resample_forecast",
        lambda values, src_interval, dst_interval: list(values),
    )

    monkeypatch.setattr(coordinator, "_refresh_battery_config", lambda: None)
    monkeypatch.setattr(
        coordinator,
        "get_current_battery_state",
        lambda: BatteryState(soc_kwh=2.0, soc_percent=25.0, power_kw=0.0, mode="idle"),
    )
    monkeypatch.setattr(coordinator, "_get_realtime_grid_w", lambda: 0.0)
    monkeypatch.setattr(
        coordinator,
        "_split_setpoint",
        lambda combined_setpoint_kw, _mode="": {"bat1": combined_setpoint_kw},
    )
    monkeypatch.setattr(coordinator._price_model, "has_data", lambda: False)
    monkeypatch.setattr(coordinator._feed_in_price_model, "has_data", lambda: False)

    hass.states.async_set("sensor.test_price", "0.20")
    hass.states.async_set("sensor.grid_consumption", "0")
    hass.states.async_set("sensor.grid_production", "0")

    live_entry = MagicMock()
    live_entry.options = {}
    monkeypatch.setattr(
        hass.config_entries,
        "async_get_entry",
        lambda entry_id: live_entry,
    )

    coordinator._committed_action = "charging"
    coordinator._committed_power = 1.2
    coordinator._committed_price = 0.20
    coordinator._committed_step_start = fixed_now

    results = [
        OptimizationResult(
            power_schedule_kw=[0.3, 0.4],
            mode_schedule=["charging", "charging"],
            soc_schedule_kwh=[2.0, 2.08],
            total_cost=0.0,
            baseline_cost=0.0,
            savings=0.0,
            optimal_power_kw=0.4,
            optimal_mode="charging",
            shadow_price_eur_kwh=0.28,
            price_forecast=[0.20, 0.20],
            pv_forecast=[0.0, 0.0],
            consumption_forecast=[0.0, 0.0],
        ),
    ]

    async def fake_async_add_executor_job(func, *args):
        return results.pop(0)

    monkeypatch.setattr(hass, "async_add_executor_job", fake_async_add_executor_job)

    data = await coordinator._run_optimization()

    # Raw DP recommendation is 0.4 kW, but the commitment filter must keep the
    # published follow_schedule setpoint locked at 1.2 kW in the same price period.
    assert data["schedule_power_kw"] == 0.4
    assert data["optimal_power_kw"] == 1.2
    assert data["control_action"]["target_power_kw"] == 1.2
    assert data["battery_setpoints"]["bat1"] == 1.2


@pytest.mark.asyncio
async def test_mode_switch_resets_commitment(hass):
    """Switching control mode must reset commitment state to prevent stale locks."""
    weather_coordinator = MagicMock()
    weather_coordinator.data = {}

    forecast_coordinator = MagicMock()
    forecast_coordinator.data = {
        "pv_forecast_kw": [0.0],
        "consumption_forecast_kw": [0.0],
        "current_pv_kw": 0.0,
        "current_dc_pv_kw": 0.0,
        "current_consumption_kw": 0.0,
    }

    config = {
        "entry_id": "test-entry",
        CONF_PRICE_SENSOR: "sensor.test_price",
        CONF_CONTROL_MODE: MODE_FOLLOW_SCHEDULE,
        CONF_OPTIMIZATION_INTERVAL_MINUTES: 15,
        CONF_FIXED_FEED_IN_PRICE: 0.04,
        CONF_POWER_CONSUMPTION_SENSORS: [],
        CONF_POWER_PRODUCTION_SENSORS: [],
        "battery_subentries": [
            (
                "bat1",
                {
                    CONF_MAX_CHARGE_POWER_KW: 1.2,
                    CONF_MAX_DISCHARGE_POWER_KW: 1.2,
                    CONF_ROUND_TRIP_EFFICIENCY: 0.90,
                    CONF_MIN_SOC_PERCENT: 10.0,
                    CONF_MAX_SOC_PERCENT: 100.0,
                    CONF_BATTERY_SOC_SENSOR: "sensor.test_soc",
                },
            )
        ],
    }

    coordinator = OptimizationCoordinator(
        hass, weather_coordinator, forecast_coordinator, config
    )

    # Simulate active commitment from a follow_schedule run
    fixed_now = datetime(2026, 3, 20, 11, 0, 0, tzinfo=timezone.utc)
    coordinator._committed_action = "charging"
    coordinator._committed_power = 1.2
    coordinator._committed_price = 0.25
    coordinator._committed_step_start = fixed_now

    # Switch to hybrid mode
    coordinator.control_mode = MODE_HYBRID

    assert coordinator._committed_action == "idle"
    assert coordinator._committed_power == 0.0
    assert coordinator._committed_price == 0.0
    assert coordinator._committed_step_start is None

    # Switch to zero_grid mode
    coordinator._committed_action = "discharging"
    coordinator._committed_power = 0.8
    coordinator.control_mode = MODE_ZERO_GRID

    assert coordinator._committed_action == "idle"
    assert coordinator._committed_power == 0.0

    # Mode switch must also reset cached setpoint so the real-time loop does
    # not apply stale setpoints from the previous mode while re-optimizing.
    coordinator._effective_mode = "zero_grid"
    coordinator._controller_schedule_w = 1200.0
    coordinator.control_mode = MODE_HYBRID

    assert coordinator._effective_mode == "idle"
    assert coordinator._controller_schedule_w == 0.0

    # Mode switch must mark the next optimization run as triggered by a mode change.
    assert coordinator._optimization_trigger_source == "mode_change"

    # Re-selecting the already-active mode is a no-op: it must NOT reset the
    # commitment filter or force the real-time loop through an idle setpoint
    # (e.g. an automation periodically calling select.select_option).
    coordinator._committed_action = "charging"
    coordinator._committed_power = 1.2
    coordinator._committed_price = 0.25
    coordinator._committed_step_start = fixed_now
    coordinator._effective_mode = "charging"
    coordinator._controller_schedule_w = 1200.0
    coordinator._optimization_trigger_source = "price_boundary"

    coordinator.control_mode = MODE_HYBRID  # same as current mode

    assert coordinator._committed_action == "charging"
    assert coordinator._committed_power == 1.2
    assert coordinator._committed_price == 0.25
    assert coordinator._committed_step_start == fixed_now
    assert coordinator._effective_mode == "charging"
    assert coordinator._controller_schedule_w == 1200.0
    assert coordinator._optimization_trigger_source == "price_boundary"


def _make_coordinator(hass):
    """Build a minimal OptimizationCoordinator for scheduling tests."""
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
                    CONF_MAX_CHARGE_POWER_KW: 1.2,
                    CONF_MAX_DISCHARGE_POWER_KW: 1.2,
                    CONF_ROUND_TRIP_EFFICIENCY: 0.92,
                    CONF_MIN_SOC_PERCENT: 10.0,
                    CONF_MAX_SOC_PERCENT: 100.0,
                    CONF_BATTERY_SOC_SENSOR: "sensor.test_soc",
                },
            )
        ],
    }
    return OptimizationCoordinator(
        hass, weather_coordinator, forecast_coordinator, config
    )


@pytest.mark.asyncio
async def test_price_period_boundary_triggers_optimization(hass, monkeypatch):
    """A new price period (start_times[0] change) must trigger optimization."""
    coordinator = _make_coordinator(hass)

    period_a = datetime(2026, 3, 21, 10, 0, 0, tzinfo=timezone.utc)
    period_b = datetime(2026, 3, 21, 11, 0, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(
        "custom_components.battery_controller.coordinator_optimization.extract_price_forecast_with_timestamps",
        lambda state: ([0.20, 0.22], [period_b, period_b + timedelta(hours=1)], 60),
    )
    monkeypatch.setattr(
        "custom_components.battery_controller.coordinator_optimization.dt_util.utcnow",
        lambda: period_b,
    )

    coordinator._last_price = 0.20
    coordinator._last_period_start = period_a  # previous period

    refresh_called = []

    async def fake_refresh():
        refresh_called.append(True)

    monkeypatch.setattr(coordinator, "async_request_refresh", fake_refresh)

    old_mock = MagicMock()
    old_mock.state = "0.20"
    new_mock = MagicMock()
    new_mock.state = "0.22"
    event = MagicMock()
    event.data = {"old_state": old_mock, "new_state": new_mock}

    coordinator._handle_price_change(event)
    await hass.async_block_till_done()

    assert refresh_called, "optimization should be triggered at period boundary"
    assert coordinator._last_period_start == period_b


@pytest.mark.asyncio
async def test_same_period_no_trigger_below_threshold(hass, monkeypatch):
    """Within the same price period, small price changes must not trigger optimization."""
    coordinator = _make_coordinator(hass)

    period = datetime(2026, 3, 21, 10, 0, 0, tzinfo=timezone.utc)
    coordinator._last_price = 0.20
    coordinator._last_period_start = period

    monkeypatch.setattr(
        "custom_components.battery_controller.coordinator_optimization.extract_price_forecast_with_timestamps",
        lambda state: ([0.201], [period], 60),
    )
    monkeypatch.setattr(
        "custom_components.battery_controller.coordinator_optimization.dt_util.utcnow",
        lambda: period + timedelta(minutes=5),
    )

    refresh_called = []

    async def fake_refresh():
        refresh_called.append(True)

    monkeypatch.setattr(coordinator, "async_request_refresh", fake_refresh)

    old_mock = MagicMock()
    old_mock.state = "0.20"
    new_mock = MagicMock()
    new_mock.state = "0.201"
    event = MagicMock()
    event.data = {"old_state": old_mock, "new_state": new_mock}

    coordinator._handle_price_change(event)
    await hass.async_block_till_done()

    assert not refresh_called, (
        "small intra-period change should not trigger optimization"
    )


@pytest.mark.asyncio
async def test_async_setup_tracks_distinct_feed_in_price_sensor(hass):
    """A feed-in price sensor different from the buy price sensor gets its own tracker."""
    from unittest.mock import AsyncMock

    weather_coordinator = MagicMock()
    weather_coordinator.data = {}
    forecast_coordinator = MagicMock()
    forecast_coordinator.data = None
    forecast_coordinator.async_add_listener = MagicMock(return_value=lambda: None)

    config = {
        "entry_id": "test-entry",
        CONF_PRICE_SENSOR: "sensor.test_price",
        CONF_FEED_IN_PRICE_SENSOR: "sensor.test_feed_in_price",
        CONF_CONTROL_MODE: MODE_FOLLOW_SCHEDULE,
        CONF_FIXED_FEED_IN_PRICE: 0.07,
        CONF_POWER_CONSUMPTION_SENSORS: [],
        CONF_POWER_PRODUCTION_SENSORS: [],
        "battery_subentries": [],
    }

    coord = OptimizationCoordinator(
        hass, weather_coordinator, forecast_coordinator, config
    )
    coord._price_model = MagicMock()
    coord._price_model.async_update_pattern = AsyncMock()
    coord._feed_in_price_model = MagicMock()
    coord._feed_in_price_model.async_update_pattern = AsyncMock()

    tracked_entities = []
    with (
        patch(
            "custom_components.battery_controller.coordinator_optimization.async_track_time_interval",
            return_value=lambda: None,
        ),
        patch(
            "custom_components.battery_controller.coordinator_optimization.async_track_state_change_event",
            side_effect=lambda h, ids, cb: (
                tracked_entities.extend(ids) or (lambda: None)
            ),
        ),
    ):
        await coord.async_setup()

    assert "sensor.test_price" in tracked_entities
    assert "sensor.test_feed_in_price" in tracked_entities


@pytest.mark.asyncio
async def test_async_setup_skips_feed_in_tracker_when_same_as_price_sensor(hass):
    """No duplicate tracker is registered when feed-in price reuses the buy price sensor."""
    from unittest.mock import AsyncMock

    weather_coordinator = MagicMock()
    weather_coordinator.data = {}
    forecast_coordinator = MagicMock()
    forecast_coordinator.data = None
    forecast_coordinator.async_add_listener = MagicMock(return_value=lambda: None)

    config = {
        "entry_id": "test-entry",
        CONF_PRICE_SENSOR: "sensor.test_price",
        CONF_FEED_IN_PRICE_SENSOR: "sensor.test_price",
        CONF_CONTROL_MODE: MODE_FOLLOW_SCHEDULE,
        CONF_FIXED_FEED_IN_PRICE: 0.07,
        CONF_POWER_CONSUMPTION_SENSORS: [],
        CONF_POWER_PRODUCTION_SENSORS: [],
        "battery_subentries": [],
    }

    coord = OptimizationCoordinator(
        hass, weather_coordinator, forecast_coordinator, config
    )
    coord._price_model = MagicMock()
    coord._price_model.async_update_pattern = AsyncMock()
    coord._feed_in_price_model = MagicMock()
    coord._feed_in_price_model.async_update_pattern = AsyncMock()

    tracked_calls = []
    with (
        patch(
            "custom_components.battery_controller.coordinator_optimization.async_track_time_interval",
            return_value=lambda: None,
        ),
        patch(
            "custom_components.battery_controller.coordinator_optimization.async_track_state_change_event",
            side_effect=lambda h, ids, cb: tracked_calls.append(ids) or (lambda: None),
        ),
    ):
        await coord.async_setup()

    # Only the buy price sensor is tracked — no duplicate for feed-in.
    assert tracked_calls == [["sensor.test_price"]]


def test_handle_feed_in_price_change_no_new_state(hass):
    """_handle_feed_in_price_change returns early when new_state is None."""
    coord = _make_coordinator(hass)
    event = MagicMock()
    event.data = {"new_state": None, "old_state": None}
    coord._handle_feed_in_price_change(event)  # Should not raise


def test_handle_feed_in_price_change_unavailable_state(hass):
    """_handle_feed_in_price_change ignores unavailable/unknown states."""
    coord = _make_coordinator(hass)
    new_state = MagicMock()
    new_state.state = "unavailable"
    event = MagicMock()
    event.data = {"new_state": new_state, "old_state": None}
    coord._handle_feed_in_price_change(event)  # Should not raise


@pytest.mark.asyncio
async def test_handle_feed_in_price_change_period_boundary_triggers(hass, monkeypatch):
    """A new feed-in price period must trigger optimization even when the buy
    price sensor is a separate, unchanged entity."""
    coordinator = _make_coordinator(hass)

    period_a = datetime(2026, 3, 21, 10, 0, 0, tzinfo=timezone.utc)
    period_b = datetime(2026, 3, 21, 11, 0, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(
        "custom_components.battery_controller.coordinator_optimization.extract_price_forecast_with_timestamps",
        lambda state: ([0.05], [period_b], 60),
    )

    coordinator._last_feed_in_period_start = period_a

    refresh_called = []

    async def fake_refresh():
        refresh_called.append(True)

    monkeypatch.setattr(coordinator, "async_request_refresh", fake_refresh)

    old_state = MagicMock()
    old_state.state = "0.10"
    new_state = MagicMock()
    new_state.state = "0.05"
    event = MagicMock()
    event.data = {"old_state": old_state, "new_state": new_state}

    coordinator._handle_feed_in_price_change(event)
    await hass.async_block_till_done()

    assert refresh_called, (
        "optimization should be triggered at feed-in price period boundary"
    )
    assert coordinator._last_feed_in_period_start == period_b


@pytest.mark.asyncio
async def test_handle_feed_in_price_change_same_period_no_trigger(hass, monkeypatch):
    """No refresh is triggered when the feed-in price period hasn't changed."""
    coordinator = _make_coordinator(hass)
    period = datetime(2026, 3, 21, 10, 0, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(
        "custom_components.battery_controller.coordinator_optimization.extract_price_forecast_with_timestamps",
        lambda state: ([0.05], [period], 60),
    )

    coordinator._last_feed_in_period_start = period

    refresh_called = []

    async def fake_refresh():
        refresh_called.append(True)

    monkeypatch.setattr(coordinator, "async_request_refresh", fake_refresh)

    old_state = MagicMock()
    old_state.state = "0.05"
    new_state = MagicMock()
    new_state.state = "0.05"
    event = MagicMock()
    event.data = {"old_state": old_state, "new_state": new_state}

    coordinator._handle_feed_in_price_change(event)
    await hass.async_block_till_done()

    assert not refresh_called


@pytest.mark.asyncio
async def test_handle_feed_in_price_change_was_unavailable_triggers(hass, monkeypatch):
    """Sensor becoming available for the first time triggers a refresh."""
    coordinator = _make_coordinator(hass)
    period = datetime(2026, 3, 21, 10, 0, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(
        "custom_components.battery_controller.coordinator_optimization.extract_price_forecast_with_timestamps",
        lambda state: ([0.05], [period], 60),
    )

    refresh_called = []

    async def fake_refresh():
        refresh_called.append(True)

    monkeypatch.setattr(coordinator, "async_request_refresh", fake_refresh)

    old_state = MagicMock()
    old_state.state = "unavailable"
    new_state = MagicMock()
    new_state.state = "0.05"
    event = MagicMock()
    event.data = {"old_state": old_state, "new_state": new_state}

    coordinator._handle_feed_in_price_change(event)
    await hass.async_block_till_done()

    assert refresh_called


def test_schedule_mid_period_run_future(hass, monkeypatch):
    """Mid-period timer is registered when mid-point is in the future."""
    coordinator = _make_coordinator(hass)

    period_start = datetime(2026, 3, 21, 11, 0, 0, tzinfo=timezone.utc)
    now = datetime(2026, 3, 21, 11, 5, 0, tzinfo=timezone.utc)  # 5 min into period

    monkeypatch.setattr(
        "custom_components.battery_controller.coordinator_optimization.dt_util.utcnow",
        lambda: now,
    )

    registered = []
    with patch(
        "custom_components.battery_controller.coordinator_optimization.async_track_point_in_time",
        side_effect=lambda hass, cb, t: registered.append(t) or (lambda: None),
    ):
        coordinator._schedule_mid_period_run(period_start, 60)

    assert len(registered) == 1
    assert registered[0] == period_start + timedelta(minutes=30)


def test_schedule_mid_period_run_past(hass, monkeypatch):
    """Mid-period timer is skipped when mid-point is already past."""
    coordinator = _make_coordinator(hass)

    period_start = datetime(2026, 3, 21, 11, 0, 0, tzinfo=timezone.utc)
    now = datetime(2026, 3, 21, 11, 35, 0, tzinfo=timezone.utc)  # past the 30-min mid

    monkeypatch.setattr(
        "custom_components.battery_controller.coordinator_optimization.dt_util.utcnow",
        lambda: now,
    )

    registered = []
    with patch(
        "custom_components.battery_controller.coordinator_optimization.async_track_point_in_time",
        side_effect=lambda hass, cb, t: registered.append(t) or (lambda: None),
    ):
        coordinator._schedule_mid_period_run(period_start, 60)

    assert not registered, "should not schedule a timer when mid-point is past"


def test_schedule_mid_period_run_cancels_previous(hass, monkeypatch):
    """Scheduling a new mid-period run cancels any existing timer."""
    coordinator = _make_coordinator(hass)

    cancelled = []
    coordinator._unsub_mid_period_timer = lambda: cancelled.append(True)

    period_start = datetime(2026, 3, 21, 11, 0, 0, tzinfo=timezone.utc)
    now = datetime(2026, 3, 21, 11, 5, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(
        "custom_components.battery_controller.coordinator_optimization.dt_util.utcnow",
        lambda: now,
    )

    with patch(
        "custom_components.battery_controller.coordinator_optimization.async_track_point_in_time",
        return_value=lambda: None,
    ):
        coordinator._schedule_mid_period_run(period_start, 60)

    assert cancelled, "previous mid-period timer must be cancelled"


# ---------------------------------------------------------------------------
# Additional coverage: simple properties and methods in coordinator_optimization.py
# ---------------------------------------------------------------------------


def test_control_mode_getter(hass):
    """control_mode property getter returns _control_mode (line 206)."""
    coord = _make_coordinator(hass)
    coord._control_mode = MODE_HYBRID
    assert coord.control_mode == MODE_HYBRID


def test_last_failure_reason_getter(hass):
    """last_failure_reason property returns _last_failure_reason (line 220)."""
    coord = _make_coordinator(hass)
    coord._last_failure_reason = "sensor unavailable"
    assert coord.last_failure_reason == "sensor unavailable"


def test_last_success_time_getter(hass):
    """last_success_time property returns _last_success_time (line 225)."""
    from datetime import datetime, timezone

    coord = _make_coordinator(hass)
    t = datetime(2026, 3, 21, 10, 0, 0, tzinfo=timezone.utc)
    coord._last_success_time = t
    assert coord.last_success_time == t


def test_optimization_enabled_getter(hass):
    """optimization_enabled property getter returns _optimization_enabled (line 230)."""
    coord = _make_coordinator(hass)
    coord._optimization_enabled = False
    assert coord.optimization_enabled is False


def test_optimization_enabled_setter(hass):
    """optimization_enabled setter updates _optimization_enabled (line 235)."""
    coord = _make_coordinator(hass)
    coord.optimization_enabled = False
    assert coord._optimization_enabled is False
    coord.optimization_enabled = True
    assert coord._optimization_enabled is True


@pytest.mark.asyncio
async def test_handle_price_model_refresh(hass):
    """_handle_price_model_refresh calls async_update_pattern on both models (239-241)."""
    from unittest.mock import AsyncMock

    coord = _make_coordinator(hass)
    coord._price_model.async_update_pattern = AsyncMock()
    coord._feed_in_price_model.async_update_pattern = AsyncMock()

    from datetime import datetime, timezone

    await coord._handle_price_model_refresh(datetime.now(timezone.utc))

    coord._price_model.async_update_pattern.assert_called_once()
    coord._feed_in_price_model.async_update_pattern.assert_called_once()


def test_handle_price_change_no_new_state(hass):
    """_handle_price_change returns early when new_state is None (line 318)."""
    coord = _make_coordinator(hass)
    event = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
    event.data = {"new_state": None, "old_state": None}
    # Should not raise
    coord._handle_price_change(event)


def test_handle_price_change_unavailable_state(hass, monkeypatch):
    """_handle_price_change returns early when state is unavailable (lines 322-323)."""
    coord = _make_coordinator(hass)
    new_state = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
    new_state.state = "unavailable"
    event = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
    event.data = {"new_state": new_state, "old_state": None}
    # Should return without calling async_request_refresh
    coord._handle_price_change(event)


@pytest.mark.asyncio
async def test_handle_price_change_exception_in_extract(hass, monkeypatch):
    """_handle_price_change handles exceptions from extract_price_forecast (338-340)."""
    coord = _make_coordinator(hass)

    def raise_exception(state):
        raise RuntimeError("parse error")

    monkeypatch.setattr(
        "custom_components.battery_controller.coordinator_optimization.extract_price_forecast_with_timestamps",
        raise_exception,
    )
    monkeypatch.setattr(
        "custom_components.battery_controller.coordinator_optimization.dt_util.utcnow",
        lambda: datetime(2026, 3, 21, 10, 0, 0, tzinfo=timezone.utc),
    )

    refresh_called = []

    async def fake_refresh():
        refresh_called.append(True)

    monkeypatch.setattr(coord, "async_request_refresh", fake_refresh)

    # was_unavailable=True path (first price update) — triggers optimization even with exception
    coord._last_price = None
    new_state = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
    new_state.state = "0.20"
    event = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
    event.data = {"new_state": new_state, "old_state": None}

    coord._handle_price_change(event)
    await hass.async_block_till_done()

    # was_unavailable=True so optimization is triggered
    assert refresh_called


@pytest.mark.asyncio
async def test_handle_price_change_was_unavailable(hass, monkeypatch):
    """was_unavailable=True triggers optimization (lines 343-351)."""
    coord = _make_coordinator(hass)
    period = datetime(2026, 3, 21, 10, 0, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(
        "custom_components.battery_controller.coordinator_optimization.extract_price_forecast_with_timestamps",
        lambda state: ([0.20], [period], 60),
    )
    monkeypatch.setattr(
        "custom_components.battery_controller.coordinator_optimization.dt_util.utcnow",
        lambda: period,
    )

    refresh_called = []

    async def fake_refresh():
        refresh_called.append(True)

    monkeypatch.setattr(coord, "async_request_refresh", fake_refresh)

    # old_state is unavailable → was_unavailable = True
    coord._last_price = None  # Also makes was_unavailable=True
    old_state = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
    old_state.state = "unavailable"
    new_state = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
    new_state.state = "0.20"
    event = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
    event.data = {"new_state": new_state, "old_state": old_state}

    coord._handle_price_change(event)
    await hass.async_block_till_done()

    assert refresh_called
    assert coord._last_price == 0.20
    assert coord._last_period_start == period


def test_schedule_mid_period_run_period_start_none(hass, monkeypatch):
    """_schedule_mid_period_run with period_start=None returns immediately (line 386)."""
    coord = _make_coordinator(hass)

    registered = []
    with patch(
        "custom_components.battery_controller.coordinator_optimization.async_track_point_in_time",
        side_effect=lambda hass, cb, t: registered.append(t) or (lambda: None),
    ):
        coord._schedule_mid_period_run(None, 60)

    assert not registered


@pytest.mark.asyncio
async def test_async_shutdown_unsubscribes_all(hass):
    """async_shutdown calls all unsub callables and clears them (lines 414-429)."""
    coord = _make_coordinator(hass)

    unsub_calls = {}
    for attr in (
        "_unsub_price",
        "_unsub_feed_in_price",
        "_unsub_soc",
        "_unsub_forecast",
        "_unsub_mid_period_timer",
        "_unsub_price_model_refresh",
        "_unsub_realtime",
    ):
        calls = []
        unsub_calls[attr] = calls
        setattr(coord, attr, lambda c=calls: c.append(True))

    await coord.async_shutdown()

    for attr, calls in unsub_calls.items():
        assert calls, f"{attr} unsub was not called"
        assert getattr(coord, attr) is None


@pytest.mark.asyncio
async def test_async_shutdown_no_unsubs(hass):
    """async_shutdown is safe when no unsub callables are set."""
    coord = _make_coordinator(hass)
    # All unsub attrs should be None by default
    await coord.async_shutdown()  # Should not raise


def test_get_realtime_grid_w_no_sensors(hass):
    """_get_realtime_grid_w returns None when no power sensors configured (line 650-651)."""
    coord = _make_coordinator(hass)
    coord._power_consumption_sensors = []
    coord._power_production_sensors = []
    assert coord._get_realtime_grid_w() is None


def test_get_realtime_grid_w_with_sensors(hass):
    """_get_realtime_grid_w reads sensors and returns net grid power (lines 652-684)."""
    coord = _make_coordinator(hass)
    coord._power_consumption_sensors = ["sensor.consumption"]
    coord._power_production_sensors = ["sensor.production"]

    hass.states.async_set("sensor.consumption", "500", {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.production", "200", {"unit_of_measurement": "W"})

    result = coord._get_realtime_grid_w()
    assert result == pytest.approx(300.0)


def test_get_realtime_grid_w_kw_unit_conversion(hass):
    """_get_realtime_grid_w converts kW sensors to W (lines 664-665, 678-679)."""
    coord = _make_coordinator(hass)
    coord._power_consumption_sensors = ["sensor.consumption_kw"]
    coord._power_production_sensors = ["sensor.production_kw"]

    hass.states.async_set("sensor.consumption_kw", "0.5", {"unit_of_measurement": "kW"})
    hass.states.async_set("sensor.production_kw", "0.2", {"unit_of_measurement": "kW"})

    result = coord._get_realtime_grid_w()
    assert result == pytest.approx(300.0)


def test_get_realtime_grid_w_all_sensors_unavailable_returns_none(hass):
    """All configured sensors unavailable must yield None, not a fictitious 0 W.

    Callers (realtime loop, full optimizer run) treat None as "no live
    reading" and fall back appropriately; 0.0 would be acted upon as a real
    balanced-grid measurement.
    """
    coord = _make_coordinator(hass)
    coord._power_consumption_sensors = ["sensor.consumption"]
    coord._power_production_sensors = ["sensor.production"]

    hass.states.async_set("sensor.consumption", "unavailable")
    hass.states.async_set("sensor.production", "unknown")

    assert coord._get_realtime_grid_w() is None


def test_get_realtime_grid_w_partial_unavailable_uses_available(hass):
    """One readable sensor is enough for a (partial) grid reading."""
    coord = _make_coordinator(hass)
    coord._power_consumption_sensors = ["sensor.consumption"]
    coord._power_production_sensors = ["sensor.production"]

    hass.states.async_set("sensor.consumption", "unavailable")
    hass.states.async_set("sensor.production", "200", {"unit_of_measurement": "W"})

    assert coord._get_realtime_grid_w() == pytest.approx(-200.0)


def test_find_stale_power_sensor_fresh_sensors(hass):
    """Fresh sensors are not reported as stale."""
    coord = _make_coordinator(hass)
    coord._power_consumption_sensors = ["sensor.consumption"]
    coord._power_production_sensors = ["sensor.production"]

    hass.states.async_set("sensor.consumption", "500", {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.production", "200", {"unit_of_measurement": "W"})

    assert coord._find_stale_power_sensor(20.0) is None


def test_find_stale_power_sensor_detects_stale_production(hass, monkeypatch):
    """Production sensors are also covered by the staleness check."""
    coord = _make_coordinator(hass)
    coord._power_consumption_sensors = []
    coord._power_production_sensors = ["sensor.production"]

    hass.states.async_set("sensor.production", "200", {"unit_of_measurement": "W"})
    state_time = hass.states.get("sensor.production").last_reported
    monkeypatch.setattr(
        "custom_components.battery_controller.coordinator_optimization.dt_util.utcnow",
        lambda: state_time + timedelta(seconds=30),
    )

    stale = coord._find_stale_power_sensor(20.0)
    assert stale is not None
    sensor_id, age_s = stale
    assert sensor_id == "sensor.production"
    assert age_s == pytest.approx(30.0, abs=1.0)


def test_find_stale_power_sensor_skips_unavailable(hass, monkeypatch):
    """Unavailable sensors are excluded from the grid sum, so an old
    unavailable state must not flag the reading as stale (that would block
    every realtime update e.g. overnight)."""
    coord = _make_coordinator(hass)
    coord._power_consumption_sensors = []
    coord._power_production_sensors = ["sensor.production"]

    hass.states.async_set("sensor.production", "unavailable")
    prod_time = hass.states.get("sensor.production").last_reported
    monkeypatch.setattr(
        "custom_components.battery_controller.coordinator_optimization.dt_util.utcnow",
        lambda: prod_time + timedelta(seconds=30),
    )

    assert coord._find_stale_power_sensor(20.0) is None


def test_resolve_controller_mode(hass):
    """_resolve_controller_mode returns correct mode string (lines 616-634)."""
    from custom_components.battery_controller.const import MODE_ZERO_GRID

    coord = _make_coordinator(hass)
    coord._control_mode = MODE_ZERO_GRID
    coord._power_consumption_sensors = ["sensor.c"]

    # zero_grid effective mode → "zero_grid"
    assert coord._resolve_controller_mode("zero_grid", 100.0) == "zero_grid"

    # idle in non-follow/non-manual mode with negative grid → upgrade to zero_grid
    coord._control_mode = MODE_ZERO_GRID
    assert coord._resolve_controller_mode("idle", -50.0) == "zero_grid"

    # idle in follow_schedule mode → stays idle
    from custom_components.battery_controller.const import MODE_FOLLOW_SCHEDULE

    coord._control_mode = MODE_FOLLOW_SCHEDULE
    assert coord._resolve_controller_mode("idle", -50.0) == "idle"

    # manual mode
    assert coord._resolve_controller_mode("manual", 0.0) == "manual"

    # charging
    assert coord._resolve_controller_mode("charging", 0.0) == "follow_schedule"

    # discharging
    assert coord._resolve_controller_mode("discharging", 0.0) == "follow_schedule"


def test_split_setpoint_no_batteries(hass):
    """_split_setpoint returns empty dict when no battery subentries (line 823-824)."""
    coord = _make_coordinator(hass)
    coord._individual_battery_configs = []
    assert coord._split_setpoint(1.0) == {}


def test_split_setpoint_charging(hass):
    """_split_setpoint proportionally splits charge when SoC gap >= threshold."""
    from custom_components.battery_controller.battery_model import (
        BatteryConfig,
        BatteryState,
    )

    coord = _make_coordinator(hass)
    cfg1 = BatteryConfig(
        capacity_kwh=10.0,
        max_charge_power_kw=5.0,
        max_discharge_power_kw=5.0,
        round_trip_efficiency=0.90,
        min_soc_percent=10.0,
        max_soc_percent=90.0,
    )
    cfg2 = BatteryConfig(
        capacity_kwh=10.0,
        max_charge_power_kw=5.0,
        max_discharge_power_kw=5.0,
        round_trip_efficiency=0.90,
        min_soc_percent=10.0,
        max_soc_percent=90.0,
    )
    coord._individual_battery_configs = [("bat1", cfg1), ("bat2", cfg2)]
    coord._per_battery_states = {
        "bat1": BatteryState(soc_kwh=5.0, soc_percent=50.0, power_kw=0.0, mode="idle"),
        "bat2": BatteryState(soc_kwh=7.0, soc_percent=70.0, power_kw=0.0, mode="idle"),
    }

    result = coord._split_setpoint(2.0)  # 2 kW charging
    assert "bat1" in result
    assert "bat2" in result
    # bat1 has more headroom (9-5=4) than bat2 (9-7=2) → bat1 gets 2/3
    total = result["bat1"] + result["bat2"]
    assert total == pytest.approx(2.0, abs=0.01)


def test_split_setpoint_discharging(hass):
    """_split_setpoint sends full discharge to single battery when gap < threshold."""
    from custom_components.battery_controller.battery_model import (
        BatteryConfig,
        BatteryState,
    )

    coord = _make_coordinator(hass)
    cfg = BatteryConfig(
        capacity_kwh=10.0,
        max_charge_power_kw=5.0,
        max_discharge_power_kw=5.0,
        round_trip_efficiency=0.90,
        min_soc_percent=10.0,
        max_soc_percent=90.0,
    )
    coord._individual_battery_configs = [("bat1", cfg)]
    coord._per_battery_states = {
        "bat1": BatteryState(soc_kwh=5.0, soc_percent=50.0, power_kw=0.0, mode="idle"),
    }

    result = coord._split_setpoint(-1.0)  # 1 kW discharging
    assert result["bat1"] == pytest.approx(-1.0, abs=0.01)


def test_split_setpoint_idle(hass):
    """_split_setpoint returns zeros for idle."""
    from custom_components.battery_controller.battery_model import BatteryConfig

    coord = _make_coordinator(hass)
    cfg = BatteryConfig(
        capacity_kwh=10.0,
        max_charge_power_kw=5.0,
        max_discharge_power_kw=5.0,
        round_trip_efficiency=0.90,
        min_soc_percent=10.0,
        max_soc_percent=90.0,
    )
    coord._individual_battery_configs = [("bat1", cfg)]
    coord._per_battery_states = {}

    result = coord._split_setpoint(0.0)
    assert result["bat1"] == 0.0


def test_get_current_battery_state_no_subentries(hass):
    """get_current_battery_state returns default when no subentries (lines 771-775)."""
    coord = _make_coordinator(hass)
    coord._individual_battery_configs = []

    state = coord.get_current_battery_state()
    assert state.soc_percent == 50.0
    assert state.power_kw == 0.0


def test_get_current_battery_state_with_sensors(hass):
    """get_current_battery_state reads sensors and returns combined state (lines 776-815)."""
    from custom_components.battery_controller.battery_model import BatteryConfig
    from custom_components.battery_controller.const import CONF_BATTERY_SOC_SENSOR

    coord = _make_coordinator(hass)
    cfg = BatteryConfig(
        capacity_kwh=10.0,
        max_charge_power_kw=5.0,
        max_discharge_power_kw=5.0,
        round_trip_efficiency=0.90,
        min_soc_percent=10.0,
        max_soc_percent=90.0,
    )
    coord._individual_battery_configs = [("bat1", cfg)]
    coord._battery_subentries = [("bat1", {CONF_BATTERY_SOC_SENSOR: "sensor.soc"})]

    hass.states.async_set("sensor.soc", "60")

    state = coord.get_current_battery_state()
    assert state.soc_percent == pytest.approx(60.0)
    assert state.soc_kwh == pytest.approx(6.0)


@pytest.mark.asyncio
async def test_handle_soc_available_triggers_refresh(hass):
    """_handle_soc_available triggers refresh when SoC becomes available (lines 414-429)."""
    coord = _make_coordinator(hass)

    refresh_called = []

    async def fake_refresh():
        refresh_called.append(True)

    from unittest.mock import MagicMock

    coord.async_request_refresh = fake_refresh

    old_state = MagicMock()
    old_state.state = "unavailable"
    new_state = MagicMock()
    new_state.state = "75.0"
    event = MagicMock()
    event.data = {"new_state": new_state, "old_state": old_state}

    coord._handle_soc_available(event)
    await hass.async_block_till_done()

    assert refresh_called


def test_handle_soc_available_no_trigger_when_already_available(hass):
    """_handle_soc_available does not trigger when both old and new are available."""
    from unittest.mock import MagicMock

    coord = _make_coordinator(hass)
    refresh_called = []
    coord.async_request_refresh = lambda: refresh_called.append(True)

    old_state = MagicMock()
    old_state.state = "70.0"
    new_state = MagicMock()
    new_state.state = "75.0"
    event = MagicMock()
    event.data = {"new_state": new_state, "old_state": old_state}

    coord._handle_soc_available(event)
    assert not refresh_called


# ---------------------------------------------------------------------------
# Additional coverage tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_setup_with_power_sensors(hass):
    """async_setup with power sensors registers a realtime timer (lines 247-304)."""
    from unittest.mock import AsyncMock

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
        CONF_POWER_CONSUMPTION_SENSORS: ["sensor.grid_consumption"],
        CONF_POWER_PRODUCTION_SENSORS: [],
        "battery_subentries": [],
    }

    coord = OptimizationCoordinator(
        hass, weather_coordinator, forecast_coordinator, config
    )
    coord._price_model = MagicMock()
    coord._price_model.async_update_pattern = AsyncMock()
    coord._feed_in_price_model = MagicMock()
    coord._feed_in_price_model.async_update_pattern = AsyncMock()

    registered_intervals = []
    with (
        patch(
            "custom_components.battery_controller.coordinator_optimization.async_track_time_interval",
            side_effect=lambda h, cb, interval: (
                registered_intervals.append(interval) or (lambda: None)
            ),
        ),
        patch(
            "custom_components.battery_controller.coordinator_optimization.async_track_state_change_event",
            return_value=lambda: None,
        ),
    ):
        await coord.async_setup()

    coord._price_model.async_update_pattern.assert_called_once()
    coord._feed_in_price_model.async_update_pattern.assert_called_once()
    # price_model_refresh interval + realtime interval
    assert len(registered_intervals) == 2


@pytest.mark.asyncio
async def test_async_setup_with_soc_sensor(hass):
    """async_setup with a battery SoC sensor registers SoC state tracking (lines 266-272)."""
    from unittest.mock import AsyncMock

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
                    CONF_MAX_CHARGE_POWER_KW: 1.2,
                    CONF_MAX_DISCHARGE_POWER_KW: 1.2,
                    CONF_ROUND_TRIP_EFFICIENCY: 0.92,
                    CONF_MIN_SOC_PERCENT: 10.0,
                    CONF_MAX_SOC_PERCENT: 90.0,
                    CONF_BATTERY_SOC_SENSOR: "sensor.test_soc",
                },
            )
        ],
    }

    coord = OptimizationCoordinator(
        hass, weather_coordinator, forecast_coordinator, config
    )
    coord._price_model = MagicMock()
    coord._price_model.async_update_pattern = AsyncMock()
    coord._feed_in_price_model = MagicMock()
    coord._feed_in_price_model.async_update_pattern = AsyncMock()

    soc_tracked = []
    with (
        patch(
            "custom_components.battery_controller.coordinator_optimization.async_track_time_interval",
            return_value=lambda: None,
        ),
        patch(
            "custom_components.battery_controller.coordinator_optimization.async_track_state_change_event",
            side_effect=lambda h, ids, cb: soc_tracked.extend(ids) or (lambda: None),
        ),
    ):
        await coord.async_setup()

    assert "sensor.test_soc" in soc_tracked


@pytest.mark.asyncio
async def test_async_update_data_concurrency_guard_with_data(hass):
    """_async_update_data returns cached data when already running (lines 894-900)."""
    coord = _make_coordinator(hass)
    coord._optimization_running = True
    coord.data = {"control_mode": "cached"}

    result = await coord._async_update_data()
    assert result == {"control_mode": "cached"}
    assert coord._pending_optimization is True


@pytest.mark.asyncio
async def test_async_update_data_concurrency_guard_no_data_raises(hass):
    """_async_update_data raises UpdateFailed when running and no cached data (lines 901-904)."""
    from homeassistant.helpers.update_coordinator import UpdateFailed

    coord = _make_coordinator(hass)
    coord._optimization_running = True
    coord.data = None

    with pytest.raises(UpdateFailed):
        await coord._async_update_data()


@pytest.mark.asyncio
async def test_async_update_data_disabled_returns_cached(hass):
    """_async_update_data returns cached data when optimization disabled (lines 908-913)."""
    coord = _make_coordinator(hass)
    coord._optimization_enabled = False
    coord.data = {"control_mode": "cached_disabled"}

    result = await coord._async_update_data()
    assert result == {"control_mode": "cached_disabled"}


@pytest.mark.asyncio
async def test_async_update_data_pending_schedules_rerun(hass, monkeypatch):
    """When _pending_optimization is set during a run, a re-run task is scheduled (lines 916-921)."""
    coord = _make_coordinator(hass)
    coord._optimization_running = False
    coord._pending_optimization = False

    async def fake_run():
        coord._pending_optimization = True
        return {"control_mode": "result"}

    monkeypatch.setattr(coord, "_run_optimization", fake_run)

    tasks = []
    monkeypatch.setattr(
        coord.hass,
        "async_create_task",
        lambda coro: tasks.append(coro),
    )

    result = await coord._async_update_data()
    assert result == {"control_mode": "result"}
    assert tasks  # re-run was scheduled


def test_get_manual_setpoint_w_no_entry(hass):
    """_get_manual_setpoint_w returns default when entry not found (lines 583-586)."""
    from custom_components.battery_controller.const import (
        DEFAULT_MANUAL_POWER_SETPOINT_W,
    )

    coord = _make_coordinator(hass)
    coord.config["entry_id"] = "nonexistent-entry-id"

    result = coord._get_manual_setpoint_w()
    assert result == DEFAULT_MANUAL_POWER_SETPOINT_W


def test_resolve_controller_mode_control_mode_fallback(hass):
    """_resolve_controller_mode returns control_mode for unhandled effective modes (line 636)."""
    coord = _make_coordinator(hass)
    coord._control_mode = MODE_ZERO_GRID

    # "zero_grid" as effective_mode falls through all if-branches
    result = coord._resolve_controller_mode("zero_grid", 100.0)
    assert result == MODE_ZERO_GRID


def test_get_realtime_grid_w_value_error_skipped(hass):
    """_get_realtime_grid_w skips sensors that cannot be converted to float (lines 669-670)."""
    from custom_components.battery_controller.const import (
        CONF_POWER_CONSUMPTION_SENSORS,
    )

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
        CONF_POWER_CONSUMPTION_SENSORS: ["sensor.bad_consumption"],
        CONF_POWER_PRODUCTION_SENSORS: ["sensor.bad_production"],
        "battery_subentries": [],
    }
    coord = OptimizationCoordinator(
        hass, weather_coordinator, forecast_coordinator, config
    )

    hass.states.async_set("sensor.bad_consumption", "not_a_number")
    hass.states.async_set("sensor.bad_production", "also_bad")

    result = coord._get_realtime_grid_w()
    # Both sensors unreadable → no live reading (None), not a fictitious 0 W
    assert result is None


def test_read_battery_state_kwh_soc_unit(hass):
    """_read_battery_state reads SoC in kWh when unit_of_measurement is kWh (lines 727-728)."""
    from custom_components.battery_controller.battery_model import BatteryConfig

    coord = _make_coordinator(hass)
    cfg = BatteryConfig(
        capacity_kwh=10.0,
        max_charge_power_kw=5.0,
        max_discharge_power_kw=5.0,
        round_trip_efficiency=0.9,
        min_soc_percent=10.0,
        max_soc_percent=90.0,
    )
    subentry_data = {CONF_BATTERY_SOC_SENSOR: "sensor.soc_kwh"}
    hass.states.async_set("sensor.soc_kwh", "7.5", {"unit_of_measurement": "kWh"})

    state = coord._read_battery_state(subentry_data, cfg, 50.0)
    assert state.soc_kwh == pytest.approx(7.5)
    assert state.soc_percent == pytest.approx(75.0)


def test_read_battery_state_fallback_soc_when_unavailable(hass):
    """_read_battery_state uses fallback SoC when sensor is unavailable (lines 733-737)."""
    from custom_components.battery_controller.battery_model import BatteryConfig

    coord = _make_coordinator(hass)
    cfg = BatteryConfig(
        capacity_kwh=10.0,
        max_charge_power_kw=5.0,
        max_discharge_power_kw=5.0,
        round_trip_efficiency=0.9,
        min_soc_percent=10.0,
        max_soc_percent=90.0,
    )
    subentry_data = {CONF_BATTERY_SOC_SENSOR: "sensor.unavailable_soc"}
    hass.states.async_set("sensor.unavailable_soc", "unavailable")

    state = coord._read_battery_state(subentry_data, cfg, 65.0)
    assert state.soc_percent == pytest.approx(65.0)
    assert state.soc_kwh == pytest.approx(6.5)


def test_read_battery_state_no_soc_sensor_uses_fallback(hass):
    """_read_battery_state uses fallback SoC when no sensor key in subentry (lines 735-737)."""
    from custom_components.battery_controller.battery_model import BatteryConfig

    coord = _make_coordinator(hass)
    cfg = BatteryConfig(
        capacity_kwh=10.0,
        max_charge_power_kw=5.0,
        max_discharge_power_kw=5.0,
        round_trip_efficiency=0.9,
        min_soc_percent=10.0,
        max_soc_percent=90.0,
    )
    # No CONF_BATTERY_SOC_SENSOR in subentry_data — else branch uses fallback via get_sensor_value
    subentry_data = {}

    state = coord._read_battery_state(subentry_data, cfg, 40.0)
    assert state.soc_percent == pytest.approx(40.0)
    assert state.soc_kwh == pytest.approx(4.0)


def test_read_battery_state_charging_mode_w_unit(hass):
    """_read_battery_state detects charging mode from W-unit power sensor (line 758)."""
    from custom_components.battery_controller.battery_model import BatteryConfig
    from custom_components.battery_controller.const import CONF_BATTERY_POWER_SENSOR

    coord = _make_coordinator(hass)
    cfg = BatteryConfig(
        capacity_kwh=10.0,
        max_charge_power_kw=5.0,
        max_discharge_power_kw=5.0,
        round_trip_efficiency=0.9,
        min_soc_percent=10.0,
        max_soc_percent=90.0,
    )
    subentry_data = {
        CONF_BATTERY_SOC_SENSOR: "sensor.soc",
        CONF_BATTERY_POWER_SENSOR: "sensor.power_w",
    }
    hass.states.async_set("sensor.soc", "50")
    hass.states.async_set("sensor.power_w", "1500", {"unit_of_measurement": "W"})

    state = coord._read_battery_state(subentry_data, cfg, 50.0)
    assert state.mode == "charging"
    assert state.power_kw == pytest.approx(1.5)


def test_read_battery_state_discharging_mode_kw_unit(hass):
    """_read_battery_state detects discharging from kW-unit power sensor (line 760)."""
    from custom_components.battery_controller.battery_model import BatteryConfig
    from custom_components.battery_controller.const import CONF_BATTERY_POWER_SENSOR

    coord = _make_coordinator(hass)
    cfg = BatteryConfig(
        capacity_kwh=10.0,
        max_charge_power_kw=5.0,
        max_discharge_power_kw=5.0,
        round_trip_efficiency=0.9,
        min_soc_percent=10.0,
        max_soc_percent=90.0,
    )
    subentry_data = {
        CONF_BATTERY_SOC_SENSOR: "sensor.soc",
        CONF_BATTERY_POWER_SENSOR: "sensor.power_kw",
    }
    hass.states.async_set("sensor.soc", "50")
    hass.states.async_set("sensor.power_kw", "-2.5", {"unit_of_measurement": "kW"})

    state = coord._read_battery_state(subentry_data, cfg, 50.0)
    assert state.mode == "discharging"
    assert state.power_kw == pytest.approx(-2.5)


def test_read_battery_state_unknown_unit_warns(hass):
    """_read_battery_state logs a warning for unknown power sensor unit (lines 749-754)."""
    from custom_components.battery_controller.battery_model import BatteryConfig
    from custom_components.battery_controller.const import CONF_BATTERY_POWER_SENSOR

    coord = _make_coordinator(hass)
    cfg = BatteryConfig(
        capacity_kwh=10.0,
        max_charge_power_kw=5.0,
        max_discharge_power_kw=5.0,
        round_trip_efficiency=0.9,
        min_soc_percent=10.0,
        max_soc_percent=90.0,
    )
    subentry_data = {
        CONF_BATTERY_SOC_SENSOR: "sensor.soc",
        CONF_BATTERY_POWER_SENSOR: "sensor.power_va",
    }
    hass.states.async_set("sensor.soc", "50")
    # Value "2.5" treated as kW (unknown unit treated as kW per warning message)
    hass.states.async_set("sensor.power_va", "2.5", {"unit_of_measurement": "VA"})

    # Should not raise; treats value as kW and logs a warning
    state = coord._read_battery_state(subentry_data, cfg, 50.0)
    assert state.power_kw == pytest.approx(2.5)


def test_get_current_battery_state_charging_mode(hass):
    """get_current_battery_state returns charging when combined power > threshold (line 806)."""
    from custom_components.battery_controller.battery_model import BatteryConfig

    coord = _make_coordinator(hass)
    cfg = BatteryConfig(
        capacity_kwh=10.0,
        max_charge_power_kw=5.0,
        max_discharge_power_kw=5.0,
        round_trip_efficiency=0.9,
        min_soc_percent=10.0,
        max_soc_percent=90.0,
    )
    coord._individual_battery_configs = [("bat1", cfg)]
    coord._battery_subentries = [("bat1", {CONF_BATTERY_SOC_SENSOR: "sensor.soc"})]

    hass.states.async_set("sensor.soc", "50")
    # Simulate charging: positive power_kw (internal convention positive=charge)
    # We do this by setting per_battery_states directly after reading
    import unittest.mock as um

    with patch.object(
        coord,
        "_read_battery_state",
        return_value=um.MagicMock(
            soc_kwh=5.0, soc_percent=50.0, power_kw=2.0, mode="charging"
        ),
    ):
        state = coord.get_current_battery_state()

    assert state.mode == "charging"


def test_get_current_battery_state_discharging_mode(hass):
    """get_current_battery_state returns discharging when combined power < -threshold (line 808)."""
    from custom_components.battery_controller.battery_model import BatteryConfig
    import unittest.mock as um

    coord = _make_coordinator(hass)
    cfg = BatteryConfig(
        capacity_kwh=10.0,
        max_charge_power_kw=5.0,
        max_discharge_power_kw=5.0,
        round_trip_efficiency=0.9,
        min_soc_percent=10.0,
        max_soc_percent=90.0,
    )
    coord._individual_battery_configs = [("bat1", cfg)]
    coord._battery_subentries = [("bat1", {CONF_BATTERY_SOC_SENSOR: "sensor.soc"})]
    hass.states.async_set("sensor.soc", "50")

    with patch.object(
        coord,
        "_read_battery_state",
        return_value=um.MagicMock(
            soc_kwh=5.0, soc_percent=50.0, power_kw=-2.0, mode="discharging"
        ),
    ):
        state = coord.get_current_battery_state()

    assert state.mode == "discharging"


@pytest.mark.asyncio
async def test_handle_realtime_update_no_data_returns_early(hass):
    """_handle_realtime_update returns early when data is None (line 441-442)."""
    coord = _make_coordinator(hass)
    coord.data = None
    coord._last_result = None

    # Should not raise
    await coord._handle_realtime_update(datetime.now(timezone.utc))


@pytest.mark.asyncio
async def test_handle_realtime_update_no_grid_returns_early(hass, monkeypatch):
    """_handle_realtime_update returns early when grid sensors unavailable (lines 446-450)."""
    coord = _make_coordinator(hass)
    coord.data = {"control_action": {}}
    coord._last_result = MagicMock()

    monkeypatch.setattr(coord, "_get_realtime_grid_w", lambda: None)

    await coord._handle_realtime_update(datetime.now(timezone.utc))
    # No exception; returned early


@pytest.mark.asyncio
async def test_handle_realtime_update_stable_setpoint_updates_battery_state(
    hass, monkeypatch
):
    """Stable setpoint still updates battery state when power changes (lines 520-536)."""
    from custom_components.battery_controller.battery_model import BatteryState

    coord = _make_coordinator(hass)
    old_battery_state = BatteryState(
        soc_kwh=5.0, soc_percent=50.0, power_kw=0.0, mode="idle"
    )
    coord.data = {
        "control_action": {
            "target_power_kw": 1.0,
            "action_mode": "charging",
            "raw_target_w": 1000.0,
            "dp_schedule_w": 1000.0,
            "mode": "follow_schedule",
        },
        "battery_state": old_battery_state,
        "per_battery_states": {},
    }
    coord._last_result = MagicMock()
    coord._effective_mode = "charging"
    coord._controller_schedule_w = 1000.0

    new_battery_state = BatteryState(
        soc_kwh=5.0, soc_percent=50.0, power_kw=-1.2, mode="charging"
    )
    monkeypatch.setattr(coord, "get_current_battery_state", lambda: new_battery_state)
    monkeypatch.setattr(coord, "_get_realtime_grid_w", lambda: -200.0)
    coord.zero_grid_controller = MagicMock()
    # Same target_power_kw as cached → setpoint_stable
    coord.zero_grid_controller.get_control_action = MagicMock(
        return_value={
            "target_power_kw": 1.0,  # unchanged
            "action_mode": "charging",
            "raw_target_w": 1000.0,
            "dp_schedule_w": 1000.0,
            "mode": "follow_schedule",
        }
    )

    updated = []
    monkeypatch.setattr(coord, "async_set_updated_data", lambda d: updated.append(d))

    await coord._handle_realtime_update(datetime.now(timezone.utc))

    # Battery state should be updated even though setpoint was stable
    assert updated
    assert updated[-1]["battery_state"] is new_battery_state


@pytest.mark.asyncio
async def test_handle_realtime_update_changed_setpoint(hass, monkeypatch):
    """Changed setpoint triggers full data update with setpoint log (lines 538-571)."""
    from custom_components.battery_controller.battery_model import BatteryState

    coord = _make_coordinator(hass)
    old_state = BatteryState(soc_kwh=5.0, soc_percent=50.0, power_kw=0.0, mode="idle")
    coord.data = {
        "control_action": {
            "target_power_kw": 0.5,
            "action_mode": "idle",
            "raw_target_w": 500.0,
            "dp_schedule_w": 500.0,
            "mode": "follow_schedule",
        },
        "battery_state": old_state,
        "per_battery_states": {},
        "battery_setpoints": {},
        "optimal_power_kw": 0.5,
        "optimal_mode": "idle",
    }
    coord._last_result = MagicMock()
    coord._effective_mode = "idle"
    coord._controller_schedule_w = 500.0

    new_battery_state = BatteryState(
        soc_kwh=5.0, soc_percent=50.0, power_kw=-1.5, mode="charging"
    )
    monkeypatch.setattr(coord, "get_current_battery_state", lambda: new_battery_state)
    monkeypatch.setattr(coord, "_get_realtime_grid_w", lambda: -300.0)
    monkeypatch.setattr(coord, "_split_setpoint", lambda kw, _mode="": {"bat1": kw})
    coord.zero_grid_controller = MagicMock()
    coord.zero_grid_controller.get_control_action = MagicMock(
        return_value={
            "target_power_kw": 1.5,  # Changed from 0.5
            "action_mode": "charging",
            "raw_target_w": 1500.0,
            "dp_schedule_w": 500.0,
            "mode": "follow_schedule",
        }
    )

    updated = []
    monkeypatch.setattr(coord, "async_set_updated_data", lambda d: updated.append(d))

    await coord._handle_realtime_update(datetime.now(timezone.utc))

    assert updated
    assert updated[-1]["control_action"]["target_power_kw"] == 1.5


# ---------------------------------------------------------------------------
# Coverage: _on_forecast_update, price threshold, stale sensor, handle_realtime
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_forecast_update_triggers_refresh_when_no_data(hass):
    """_on_forecast_update triggers refresh when forecast arrives and data is None (277-278)."""
    from unittest.mock import AsyncMock

    weather_coordinator = MagicMock()
    weather_coordinator.data = {}
    forecast_coordinator = MagicMock()
    forecast_coordinator.data = {"pv_forecast_kw": [0.5]}

    listener_callbacks = []

    def mock_add_listener(callback):
        listener_callbacks.append(callback)
        return lambda: None

    forecast_coordinator.async_add_listener = mock_add_listener

    config = {
        "entry_id": "test-entry",
        CONF_PRICE_SENSOR: "sensor.test_price",
        CONF_CONTROL_MODE: MODE_FOLLOW_SCHEDULE,
        CONF_FIXED_FEED_IN_PRICE: 0.07,
        CONF_POWER_CONSUMPTION_SENSORS: [],
        CONF_POWER_PRODUCTION_SENSORS: [],
        "battery_subentries": [],
    }

    coord = OptimizationCoordinator(
        hass, weather_coordinator, forecast_coordinator, config
    )
    coord._price_model = MagicMock()
    coord._price_model.async_update_pattern = AsyncMock()
    coord._feed_in_price_model = MagicMock()
    coord._feed_in_price_model.async_update_pattern = AsyncMock()
    coord.data = None  # No data yet

    with (
        patch(
            "custom_components.battery_controller.coordinator_optimization.async_track_time_interval",
            return_value=lambda: None,
        ),
        patch(
            "custom_components.battery_controller.coordinator_optimization.async_track_state_change_event",
            return_value=lambda: None,
        ),
    ):
        await coord.async_setup()

    refresh_called = []

    async def fake_refresh():
        refresh_called.append(True)

    coord.async_request_refresh = fake_refresh

    assert listener_callbacks, "async_add_listener should have been called"
    listener_callbacks[0]()
    await hass.async_block_till_done()

    assert refresh_called


@pytest.mark.asyncio
async def test_handle_price_change_significant_threshold_triggers(hass, monkeypatch):
    """_handle_price_change re-optimizes on >=10% price change within same period (367-371)."""
    coord = _make_coordinator(hass)
    period = datetime(2026, 3, 21, 10, 0, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(
        "custom_components.battery_controller.coordinator_optimization.extract_price_forecast_with_timestamps",
        lambda state: ([0.20], [period], 60),
    )
    monkeypatch.setattr(
        "custom_components.battery_controller.coordinator_optimization.dt_util.utcnow",
        lambda: period,
    )

    refresh_called = []

    async def fake_refresh():
        refresh_called.append(True)

    monkeypatch.setattr(coord, "async_request_refresh", fake_refresh)

    # Same period — only the threshold branch fires
    coord._last_price = 0.20
    coord._last_period_start = period

    old_mock = MagicMock()
    old_mock.state = "0.20"
    new_mock = MagicMock()
    new_mock.state = "0.25"  # 25% increase → above 10% threshold
    event = MagicMock()
    event.data = {"old_state": old_mock, "new_state": new_mock}

    coord._handle_price_change(event)
    await hass.async_block_till_done()

    assert refresh_called
    assert coord._last_price == pytest.approx(0.25)


def test_resolve_controller_mode_unknown_effective_mode(hass):
    """_resolve_controller_mode returns control_mode for an unknown effective mode (line 636)."""
    coord = _make_coordinator(hass)
    coord._control_mode = MODE_HYBRID
    coord._power_consumption_sensors = []

    # Any mode string that doesn't match the known branches falls through to line 636
    result = coord._resolve_controller_mode("unknown_mode", 0.0)
    assert result == MODE_HYBRID


def test_split_setpoint_charging_zero_headroom(hass):
    """_split_setpoint returns 0 when the concentrated battery is already at max SoC."""
    from custom_components.battery_controller.battery_model import (
        BatteryConfig,
        BatteryState,
    )

    coord = _make_coordinator(hass)
    cfg = BatteryConfig(
        capacity_kwh=10.0,
        max_charge_power_kw=5.0,
        max_discharge_power_kw=5.0,
        round_trip_efficiency=0.9,
        min_soc_percent=0.0,
        max_soc_percent=100.0,
    )
    coord._individual_battery_configs = [("bat1", cfg)]
    # Battery is at max SoC → headroom = 0
    coord._per_battery_states = {
        "bat1": BatteryState(
            soc_kwh=10.0, soc_percent=100.0, power_kw=0.0, mode="idle"
        ),
    }

    result = coord._split_setpoint(1.0)
    assert result["bat1"] == pytest.approx(0.0)


def test_split_setpoint_discharging_zero_available(hass):
    """_split_setpoint returns 0 when the concentrated battery is already at min SoC."""
    from custom_components.battery_controller.battery_model import (
        BatteryConfig,
        BatteryState,
    )

    coord = _make_coordinator(hass)
    cfg = BatteryConfig(
        capacity_kwh=10.0,
        max_charge_power_kw=5.0,
        max_discharge_power_kw=5.0,
        round_trip_efficiency=0.9,
        min_soc_percent=0.0,
        max_soc_percent=100.0,
    )
    coord._individual_battery_configs = [("bat1", cfg)]
    # Battery is at min SoC → available = 0
    coord._per_battery_states = {
        "bat1": BatteryState(soc_kwh=0.0, soc_percent=0.0, power_kw=0.0, mode="idle"),
    }

    result = coord._split_setpoint(-1.0)
    assert result["bat1"] == pytest.approx(0.0)


def test_refresh_battery_config_with_entry(hass, monkeypatch):
    """_refresh_battery_config re-reads BatteryConfig from live subentry data (871-884)."""

    coord = _make_coordinator(hass)
    coord._battery_subentries = [("bat1", {})]

    mock_subentry = MagicMock()
    mock_subentry.data = {
        CONF_MAX_CHARGE_POWER_KW: 2.0,
        CONF_MAX_DISCHARGE_POWER_KW: 2.0,
        CONF_ROUND_TRIP_EFFICIENCY: 0.90,
        CONF_MIN_SOC_PERCENT: 10.0,
        CONF_MAX_SOC_PERCENT: 90.0,
    }

    mock_entry = MagicMock()
    mock_entry.subentries = {"bat1": mock_subentry}
    monkeypatch.setattr(hass.config_entries, "async_get_entry", lambda eid: mock_entry)

    coord._refresh_battery_config()

    assert len(coord._individual_battery_configs) == 1
    sid, cfg = coord._individual_battery_configs[0]
    assert sid == "bat1"
    assert cfg.max_charge_power_kw == pytest.approx(2.0)


def test_refresh_battery_config_no_entry(hass, monkeypatch):
    """_refresh_battery_config returns early when entry is not found."""
    coord = _make_coordinator(hass)
    monkeypatch.setattr(hass.config_entries, "async_get_entry", lambda eid: None)

    # Should not raise — just return
    coord._refresh_battery_config()


def test_get_manual_setpoint_w_with_entry(hass, monkeypatch):
    """_get_manual_setpoint_w reads setpoint from live entry options (587-593)."""
    from custom_components.battery_controller.const import CONF_MANUAL_POWER_SETPOINT_W

    coord = _make_coordinator(hass)

    mock_entry = MagicMock()
    mock_entry.options = {CONF_MANUAL_POWER_SETPOINT_W: 800.0}
    monkeypatch.setattr(hass.config_entries, "async_get_entry", lambda eid: mock_entry)

    result = coord._get_manual_setpoint_w()
    # Negated: user enters positive=discharge, controller expects positive=charge
    assert result == pytest.approx(-800.0)


@pytest.mark.asyncio
async def test_handle_realtime_update_stale_sensor_returns_early(hass, monkeypatch):
    """_handle_realtime_update returns early when power sensor is stale (460-472)."""
    coord = _make_coordinator(hass)
    coord._power_consumption_sensors = ["sensor.grid_w"]
    coord.data = {"control_action": {"target_power_kw": 1.0}}
    coord._last_result = MagicMock()
    coord._effective_mode = "follow_schedule"
    coord._controller_schedule_w = 1000.0

    hass.states.async_set("sensor.grid_w", "500", {"unit_of_measurement": "W"})
    state_time = hass.states.get("sensor.grid_w").last_updated

    # Mock utcnow to be 30 s later (> stale_limit_s = 2.0 × 10.0 = 20 s)
    monkeypatch.setattr(
        "custom_components.battery_controller.coordinator_optimization.dt_util.utcnow",
        lambda: state_time + timedelta(seconds=30),
    )
    monkeypatch.setattr(coord, "_get_realtime_grid_w", lambda: 500.0)

    updated = []
    monkeypatch.setattr(coord, "async_set_updated_data", lambda d: updated.append(d))

    await coord._handle_realtime_update(datetime.now(timezone.utc))

    assert not updated, "stale sensor must prevent any data update"


@pytest.mark.asyncio
async def test_stale_zero_grid_unwinds_toward_zero(hass, monkeypatch):
    """A stale (steady) grid sensor in zero_grid must still unwind an
    over-committed discharge toward zero.

    Reproduces the post-spike self-lock: after a Quooker/kettle spike the
    integrator is left discharging ~1.5 kW, which holds the grid at a steady
    export so an on-change power sensor stops updating and reads as "stale".
    The correction must not be skipped — reducing battery power changes the
    grid and wakes the sensor.
    """
    coord = _make_coordinator(hass)
    coord._control_mode = MODE_ZERO_GRID
    coord._power_consumption_sensors = ["sensor.grid_w"]
    coord._last_result = MagicMock()
    # Battery left over-discharging 1500 W after a spike.
    coord.zero_grid_controller.reset_setpoint(-1500.0)
    coord.data = {
        "control_action": {"target_power_kw": -1.5, "target_power_w": -1500.0},
        "battery_state": BatteryState(
            soc_kwh=5.0, soc_percent=50.0, power_kw=-1.5, mode="discharging"
        ),
        "per_battery_states": {},
    }

    hass.states.async_set("sensor.grid_w", "-1300", {"unit_of_measurement": "W"})
    state_time = hass.states.get("sensor.grid_w").last_updated
    monkeypatch.setattr(
        "custom_components.battery_controller.coordinator_optimization.dt_util.utcnow",
        lambda: state_time + timedelta(seconds=30),
    )
    monkeypatch.setattr(coord, "_get_realtime_grid_w", lambda: -1300.0)
    monkeypatch.setattr(
        coord,
        "get_current_battery_state",
        lambda: BatteryState(
            soc_kwh=5.0, soc_percent=50.0, power_kw=-1.5, mode="discharging"
        ),
    )
    monkeypatch.setattr(coord, "_split_setpoint", lambda kw, _mode="": {})
    updated = []
    monkeypatch.setattr(coord, "async_set_updated_data", lambda d: updated.append(d))

    await coord._handle_realtime_update(datetime.now(timezone.utc))

    assert updated, "stale steady grid must still correct an over-commitment"
    new_w = updated[-1]["control_action"]["target_power_w"]
    # Moved toward zero (covers only base load now), not held at -1500 W.
    assert -1500.0 < new_w <= 0.0
    assert abs(new_w) < 1500.0
    # The control mode is still zero_grid — not the instantaneous action.
    assert updated[-1]["optimal_mode"] == "zero_grid"


@pytest.mark.asyncio
async def test_realtime_zero_grid_reports_zero_grid_mode(hass, monkeypatch):
    """In zero_grid the real-time loop must report optimal_mode='zero_grid',
    not the instantaneous physical action, so the Optimal Mode sensor does not
    flip to 'discharging'/'charging' between optimizer runs.
    """
    coord = _make_coordinator(hass)
    coord._control_mode = MODE_ZERO_GRID
    coord._power_consumption_sensors = ["sensor.grid_w"]
    coord._last_result = MagicMock()
    coord.zero_grid_controller.reset_setpoint(0.0)
    coord.data = {
        "control_action": {"target_power_kw": 0.0, "target_power_w": 0.0},
        "battery_state": BatteryState(
            soc_kwh=5.0, soc_percent=50.0, power_kw=0.0, mode="idle"
        ),
        "per_battery_states": {},
    }

    # Fresh sensor reading (not stale): importing 500 W → controller discharges.
    hass.states.async_set("sensor.grid_w", "500", {"unit_of_measurement": "W"})
    monkeypatch.setattr(coord, "_get_realtime_grid_w", lambda: 500.0)
    monkeypatch.setattr(
        coord,
        "get_current_battery_state",
        lambda: BatteryState(soc_kwh=5.0, soc_percent=50.0, power_kw=0.0, mode="idle"),
    )
    monkeypatch.setattr(coord, "_split_setpoint", lambda kw, _mode="": {})
    updated = []
    monkeypatch.setattr(coord, "async_set_updated_data", lambda d: updated.append(d))

    await coord._handle_realtime_update(datetime.now(timezone.utc))

    assert updated, "a setpoint change must publish an update"
    # Physical action is discharging (covers the load) ...
    assert updated[-1]["control_action"]["action_mode"] == "discharging"
    # ... but the reported control mode stays zero_grid.
    assert updated[-1]["optimal_mode"] == "zero_grid"


@pytest.mark.asyncio
async def test_stale_zero_grid_rejects_runaway(hass, monkeypatch):
    """A stale (possibly dead/frozen) grid sensor must never drive the battery
    into a larger discharge — the original P4.1 runaway protection is kept."""
    coord = _make_coordinator(hass)
    coord._control_mode = MODE_ZERO_GRID
    coord._power_consumption_sensors = ["sensor.grid_w"]
    coord._last_result = MagicMock()
    coord.zero_grid_controller.reset_setpoint(-200.0)
    coord.data = {
        "control_action": {"target_power_kw": -0.2, "target_power_w": -200.0},
        "battery_state": BatteryState(
            soc_kwh=5.0, soc_percent=50.0, power_kw=-0.2, mode="discharging"
        ),
        "per_battery_states": {},
    }

    # Sensor frozen at a large import value.
    hass.states.async_set("sensor.grid_w", "2000", {"unit_of_measurement": "W"})
    state_time = hass.states.get("sensor.grid_w").last_updated
    monkeypatch.setattr(
        "custom_components.battery_controller.coordinator_optimization.dt_util.utcnow",
        lambda: state_time + timedelta(seconds=30),
    )
    monkeypatch.setattr(coord, "_get_realtime_grid_w", lambda: 2000.0)
    monkeypatch.setattr(
        coord,
        "get_current_battery_state",
        lambda: BatteryState(
            soc_kwh=5.0, soc_percent=50.0, power_kw=-0.2, mode="discharging"
        ),
    )
    monkeypatch.setattr(coord, "_split_setpoint", lambda kw, _mode="": {})
    updated = []
    monkeypatch.setattr(coord, "async_set_updated_data", lambda d: updated.append(d))

    await coord._handle_realtime_update(datetime.now(timezone.utc))

    assert not updated, "stale frozen sensor must not drive a larger discharge"
    # Integrator memory restored — not wound up further.
    assert coord.zero_grid_controller.last_target_w == pytest.approx(-200.0)


@pytest.mark.asyncio
async def test_handle_realtime_update_mode_zero_grid(hass, monkeypatch):
    """_handle_realtime_update uses zero_grid schedule when control mode is zero_grid (481-482)."""
    coord = _make_coordinator(hass)
    coord._control_mode = MODE_ZERO_GRID
    coord._effective_mode = "charging"
    coord._controller_schedule_w = 1200.0
    coord.data = {
        "control_action": {
            "target_power_kw": 1.0,
            "action_mode": "charging",
            "raw_target_w": 1000.0,
            "dp_schedule_w": 1000.0,
            "mode": "follow_schedule",
        },
        "battery_state": BatteryState(
            soc_kwh=5.0, soc_percent=50.0, power_kw=0.0, mode="idle"
        ),
        "per_battery_states": {},
    }
    coord._last_result = MagicMock()

    coord.zero_grid_controller = MagicMock()
    captured = {}
    coord.zero_grid_controller.get_control_action = MagicMock(
        side_effect=lambda **kw: (
            captured.update(kw)
            or {
                "target_power_kw": 0.0,
                "target_power_w": 0.0,
                "action_mode": "zero_grid",
                "raw_target_w": 0.0,
                "dp_schedule_w": 0.0,
                "mode": "zero_grid",
            }
        )
    )
    monkeypatch.setattr(
        coord,
        "get_current_battery_state",
        lambda: BatteryState(soc_kwh=5.0, soc_percent=50.0, power_kw=0.0, mode="idle"),
    )
    monkeypatch.setattr(coord, "_get_realtime_grid_w", lambda: 50.0)
    monkeypatch.setattr(coord, "_split_setpoint", lambda kw, _mode="": {})
    monkeypatch.setattr(coord, "async_set_updated_data", lambda d: None)

    await coord._handle_realtime_update(datetime.now(timezone.utc))

    # Zero-grid mode: dp_schedule_w must be 0
    assert captured.get("dp_schedule_w") == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_handle_realtime_update_mode_manual(hass, monkeypatch):
    """_handle_realtime_update reads live manual setpoint in MODE_MANUAL (485-488)."""
    from custom_components.battery_controller.const import MODE_MANUAL

    coord = _make_coordinator(hass)
    coord._control_mode = MODE_MANUAL
    coord._effective_mode = "idle"
    coord._controller_schedule_w = 0.0
    coord.data = {
        "control_action": {
            "target_power_kw": 0.0,
            "action_mode": "idle",
            "raw_target_w": 0.0,
            "dp_schedule_w": 0.0,
            "mode": "manual",
        },
        "battery_state": BatteryState(
            soc_kwh=5.0, soc_percent=50.0, power_kw=0.0, mode="idle"
        ),
        "per_battery_states": {},
    }
    coord._last_result = MagicMock()

    monkeypatch.setattr(coord, "_get_manual_setpoint_w", lambda: -1000.0)

    coord.zero_grid_controller = MagicMock()
    captured = {}
    coord.zero_grid_controller.get_control_action = MagicMock(
        side_effect=lambda **kw: (
            captured.update(kw)
            or {
                "target_power_kw": -1.0,
                "target_power_w": -1000.0,
                "action_mode": "manual",
                "raw_target_w": -1000.0,
                "dp_schedule_w": -1000.0,
                "mode": "manual",
            }
        )
    )
    monkeypatch.setattr(
        coord,
        "get_current_battery_state",
        lambda: BatteryState(
            soc_kwh=5.0, soc_percent=50.0, power_kw=-1.0, mode="discharging"
        ),
    )
    monkeypatch.setattr(coord, "_get_realtime_grid_w", lambda: 100.0)
    monkeypatch.setattr(coord, "_split_setpoint", lambda kw, _mode="": {})
    monkeypatch.setattr(coord, "async_set_updated_data", lambda d: None)

    await coord._handle_realtime_update(datetime.now(timezone.utc))

    # Manual mode: schedule_w must come from _get_manual_setpoint_w (-1000 W)
    assert captured.get("dp_schedule_w") == pytest.approx(-1000.0)


# ---------------------------------------------------------------------------
# Coverage: _run_optimization paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_optimization_no_forecast_data_raises(hass, monkeypatch):
    """_run_optimization raises UpdateFailed when forecast data is None (943-946)."""
    from homeassistant.helpers.update_coordinator import UpdateFailed

    coord = _make_coordinator(hass)
    coord.forecast_coordinator.data = None
    monkeypatch.setattr(coord, "_refresh_battery_config", lambda: None)

    with pytest.raises(UpdateFailed):
        await coord._run_optimization()


@pytest.mark.asyncio
async def test_run_optimization_no_price_sensor_raises(hass, monkeypatch):
    """_run_optimization raises UpdateFailed when no price sensor configured (955-958)."""
    from homeassistant.helpers.update_coordinator import UpdateFailed

    coord = _make_coordinator(hass)
    coord._price_sensor = ""
    coord.forecast_coordinator.data = {
        "pv_forecast_kw": [0.0],
        "consumption_forecast_kw": [0.5],
        "current_pv_kw": 0.0,
        "current_dc_pv_kw": 0.0,
        "current_consumption_kw": 0.5,
    }
    monkeypatch.setattr(coord, "_refresh_battery_config", lambda: None)

    with pytest.raises(UpdateFailed):
        await coord._run_optimization()


@pytest.mark.asyncio
async def test_run_optimization_price_sensor_unavailable_creates_issue(
    hass, monkeypatch
):
    """Unavailable price sensor without model data raises UpdateFailed and creates an issue."""
    from homeassistant.helpers.update_coordinator import UpdateFailed

    coord = _make_coordinator(hass)
    coord.forecast_coordinator.data = {
        "pv_forecast_kw": [0.0],
        "consumption_forecast_kw": [0.5],
        "current_pv_kw": 0.0,
        "current_dc_pv_kw": 0.0,
        "current_consumption_kw": 0.5,
    }
    monkeypatch.setattr(coord, "_refresh_battery_config", lambda: None)
    monkeypatch.setattr(coord._price_model, "has_data", lambda: False)
    hass.states.async_set("sensor.test_price", "unavailable")

    with patch(
        "custom_components.battery_controller.coordinator_optimization.ir.async_create_issue"
    ) as mock_create_issue:
        with pytest.raises(UpdateFailed):
            await coord._run_optimization()

    mock_create_issue.assert_called_once()
    assert "not available" in coord.last_failure_reason


@pytest.mark.asyncio
async def test_run_optimization_price_sensor_unavailable_uses_model_fallback(
    hass, monkeypatch
):
    """Unavailable live price sensor should fall back to the historical price model."""
    from unittest.mock import patch as upatch

    coord = _make_coordinator(hass)
    fixed_now = datetime(2026, 3, 21, 10, 0, 0, tzinfo=timezone.utc)
    coord.forecast_coordinator.data = {
        "pv_forecast_kw": [0.0, 0.0],
        "consumption_forecast_kw": [0.5, 0.5],
        "current_pv_kw": 0.0,
        "current_dc_pv_kw": 0.0,
        "current_consumption_kw": 0.5,
    }
    hass.states.async_set("sensor.test_price", "unavailable")
    monkeypatch.setattr(coord, "_refresh_battery_config", lambda: None)
    monkeypatch.setattr(
        coord,
        "get_current_battery_state",
        lambda: BatteryState(soc_kwh=5.0, soc_percent=50.0, power_kw=0.0, mode="idle"),
    )
    monkeypatch.setattr(coord, "_get_realtime_grid_w", lambda: 0.0)
    monkeypatch.setattr(coord, "_split_setpoint", lambda kw, _mode="": {"bat1": kw})
    monkeypatch.setattr(coord._price_model, "has_data", lambda: True)
    monkeypatch.setattr(
        coord._price_model,
        "forecast",
        lambda hours, **kwargs: [0.21 + 0.01 * i for i in range(hours)],
    )
    monkeypatch.setattr(coord._feed_in_price_model, "has_data", lambda: False)
    monkeypatch.setattr(
        "custom_components.battery_controller.coordinator_optimization.compute_step_durations_hours",
        lambda *a: [1.0, 1.0],
    )
    monkeypatch.setattr(
        "custom_components.battery_controller.coordinator_optimization.resample_forecast",
        lambda values, src, dst: list(values),
    )
    monkeypatch.setattr(
        "custom_components.battery_controller.coordinator_optimization.dt_util.utcnow",
        lambda: fixed_now,
    )
    monkeypatch.setattr(
        "custom_components.battery_controller.coordinator_optimization.dt_util.now",
        lambda: fixed_now,
    )
    live_entry = MagicMock()
    live_entry.options = {}
    monkeypatch.setattr(hass.config_entries, "async_get_entry", lambda eid: live_entry)

    captured = {}

    def fake_optimize(*args):
        captured["prices"] = args[2]
        return OptimizationResult(
            power_schedule_kw=[0.0, 0.0],
            mode_schedule=["idle", "idle"],
            soc_schedule_kwh=[5.0, 5.0, 5.0],
            total_cost=0.0,
            baseline_cost=0.0,
            savings=0.0,
            optimal_power_kw=0.0,
            optimal_mode="idle",
            shadow_price_eur_kwh=0.15,
            price_forecast=args[2],
            pv_forecast=[0.0, 0.0],
            consumption_forecast=[0.5, 0.5],
        )

    coord.zero_grid_controller = MagicMock()
    coord.zero_grid_controller.get_control_action = MagicMock(
        return_value={
            "target_power_kw": 0.0,
            "target_power_w": 0.0,
            "action_mode": "idle",
            "raw_target_w": 0.0,
            "dp_schedule_w": 0.0,
            "mode": "idle",
        }
    )

    with upatch(
        "custom_components.battery_controller.coordinator_optimization.optimize_battery_schedule",
        side_effect=fake_optimize,
    ):
        data = await coord._run_optimization()

    assert captured["prices"][:2] == [0.21, 0.22]
    assert len(captured["prices"]) >= 2
    assert data["price_forecast_source"] == "historical_model"


@pytest.mark.asyncio
async def test_run_optimization_feed_in_sensor_unavailable_uses_fixed_price(
    hass, monkeypatch
):
    """Unavailable feed-in price sensor should fall back to the configured fixed price."""
    from unittest.mock import patch as upatch

    coord = _make_coordinator(hass)
    fixed_now = datetime(2026, 3, 21, 10, 0, 0, tzinfo=timezone.utc)
    coord.config[CONF_FEED_IN_PRICE_SENSOR] = "sensor.feed_in"
    coord.config[CONF_FIXED_FEED_IN_PRICE] = 0.07
    coord.forecast_coordinator.data = {
        "pv_forecast_kw": [0.0, 0.0],
        "consumption_forecast_kw": [0.5, 0.5],
        "current_pv_kw": 0.0,
        "current_dc_pv_kw": 0.0,
        "current_consumption_kw": 0.5,
    }
    hass.states.async_set("sensor.test_price", "0.20")
    hass.states.async_set("sensor.feed_in", "unavailable")
    monkeypatch.setattr(coord, "_refresh_battery_config", lambda: None)
    monkeypatch.setattr(
        coord,
        "get_current_battery_state",
        lambda: BatteryState(soc_kwh=5.0, soc_percent=50.0, power_kw=0.0, mode="idle"),
    )
    monkeypatch.setattr(coord, "_get_realtime_grid_w", lambda: 0.0)
    monkeypatch.setattr(coord, "_split_setpoint", lambda kw, _mode="": {"bat1": kw})
    monkeypatch.setattr(coord._price_model, "has_data", lambda: False)
    monkeypatch.setattr(coord._feed_in_price_model, "has_data", lambda: False)
    monkeypatch.setattr(
        "custom_components.battery_controller.coordinator_optimization.extract_price_forecast_with_timestamps",
        lambda state: ([0.20, 0.22], [fixed_now, fixed_now + timedelta(hours=1)], 60),
    )
    monkeypatch.setattr(
        "custom_components.battery_controller.coordinator_optimization.compute_step_durations_hours",
        lambda *a: [1.0, 1.0],
    )
    monkeypatch.setattr(
        "custom_components.battery_controller.coordinator_optimization.resample_forecast",
        lambda values, src, dst: list(values),
    )
    monkeypatch.setattr(
        "custom_components.battery_controller.coordinator_optimization.dt_util.utcnow",
        lambda: fixed_now,
    )
    monkeypatch.setattr(
        "custom_components.battery_controller.coordinator_optimization.dt_util.now",
        lambda: fixed_now,
    )
    live_entry = MagicMock()
    live_entry.options = {}
    monkeypatch.setattr(hass.config_entries, "async_get_entry", lambda eid: live_entry)

    captured = {}

    def fake_optimize(*args):
        captured["feed_in"] = args[3]
        return OptimizationResult(
            power_schedule_kw=[0.0, 0.0],
            mode_schedule=["idle", "idle"],
            soc_schedule_kwh=[5.0, 5.0, 5.0],
            total_cost=0.0,
            baseline_cost=0.0,
            savings=0.0,
            optimal_power_kw=0.0,
            optimal_mode="idle",
            shadow_price_eur_kwh=0.15,
            price_forecast=args[2],
            pv_forecast=[0.0, 0.0],
            consumption_forecast=[0.5, 0.5],
        )

    coord.zero_grid_controller = MagicMock()
    coord.zero_grid_controller.get_control_action = MagicMock(
        return_value={
            "target_power_kw": 0.0,
            "target_power_w": 0.0,
            "action_mode": "idle",
            "raw_target_w": 0.0,
            "dp_schedule_w": 0.0,
            "mode": "idle",
        }
    )

    with upatch(
        "custom_components.battery_controller.coordinator_optimization.optimize_battery_schedule",
        side_effect=fake_optimize,
    ):
        await coord._run_optimization()

    assert captured["feed_in"] == [0.07, 0.07]


@pytest.mark.asyncio
async def test_run_optimization_mode_zero_grid(hass, monkeypatch):
    """_run_optimization with MODE_ZERO_GRID sets effective_mode to zero_grid (1364-1365)."""
    from unittest.mock import patch as upatch

    coord = _make_coordinator(hass)
    coord._control_mode = MODE_ZERO_GRID

    fixed_now = datetime(2026, 3, 21, 10, 0, 0, tzinfo=timezone.utc)
    prices = [0.20, 0.22]
    price_times = [fixed_now, fixed_now + timedelta(hours=1)]

    coord.forecast_coordinator.data = {
        "pv_forecast_kw": [0.0, 0.0],
        "consumption_forecast_kw": [0.5, 0.5],
        "current_pv_kw": 0.0,
        "current_dc_pv_kw": 0.0,
        "current_consumption_kw": 0.5,
    }
    hass.states.async_set("sensor.test_price", "0.20")

    monkeypatch.setattr(
        "custom_components.battery_controller.coordinator_optimization.extract_price_forecast_with_timestamps",
        lambda state: (prices, price_times, 60),
    )
    monkeypatch.setattr(
        "custom_components.battery_controller.coordinator_optimization.compute_step_durations_hours",
        lambda *a: [1.0, 1.0],
    )
    monkeypatch.setattr(
        "custom_components.battery_controller.coordinator_optimization.resample_forecast",
        lambda values, src, dst: list(values),
    )
    monkeypatch.setattr(
        "custom_components.battery_controller.coordinator_optimization.dt_util.utcnow",
        lambda: fixed_now,
    )
    monkeypatch.setattr(
        "custom_components.battery_controller.coordinator_optimization.dt_util.now",
        lambda: fixed_now,
    )
    monkeypatch.setattr(coord, "_refresh_battery_config", lambda: None)
    monkeypatch.setattr(
        coord,
        "get_current_battery_state",
        lambda: BatteryState(soc_kwh=5.0, soc_percent=50.0, power_kw=0.0, mode="idle"),
    )
    monkeypatch.setattr(coord, "_get_realtime_grid_w", lambda: 50.0)
    monkeypatch.setattr(coord, "_split_setpoint", lambda kw, _mode="": {"bat1": kw})
    monkeypatch.setattr(coord._price_model, "has_data", lambda: False)
    monkeypatch.setattr(coord._feed_in_price_model, "has_data", lambda: False)

    live_entry = MagicMock()
    live_entry.options = {}
    monkeypatch.setattr(hass.config_entries, "async_get_entry", lambda eid: live_entry)

    fake_result = OptimizationResult(
        power_schedule_kw=[0.0, 0.0],
        mode_schedule=["idle", "idle"],
        soc_schedule_kwh=[5.0, 5.0, 5.0],
        total_cost=0.0,
        baseline_cost=0.0,
        savings=0.0,
        optimal_power_kw=0.0,
        optimal_mode="idle",
        shadow_price_eur_kwh=0.15,
        price_forecast=prices,
        pv_forecast=[0.0, 0.0],
        consumption_forecast=[0.5, 0.5],
    )

    coord.zero_grid_controller = MagicMock()
    coord.zero_grid_controller.get_control_action = MagicMock(
        return_value={
            "target_power_kw": 0.0,
            "target_power_w": 0.0,
            "action_mode": "zero_grid",
            "raw_target_w": 0.0,
            "dp_schedule_w": 0.0,
            "mode": "zero_grid",
        }
    )

    with upatch(
        "custom_components.battery_controller.coordinator_optimization.optimize_battery_schedule",
        return_value=fake_result,
    ):
        data = await coord._run_optimization()

    assert data["optimal_mode"] == "zero_grid"
    assert data["optimal_power_kw"] == pytest.approx(0.0)


@pytest.mark.asyncio
async def test_run_optimization_mode_manual(hass, monkeypatch):
    """_run_optimization with MODE_MANUAL reads live setpoint (1367-1369)."""
    from unittest.mock import patch as upatch
    from custom_components.battery_controller.const import MODE_MANUAL

    coord = _make_coordinator(hass)
    coord._control_mode = MODE_MANUAL

    fixed_now = datetime(2026, 3, 21, 10, 0, 0, tzinfo=timezone.utc)
    prices = [0.20, 0.22]
    price_times = [fixed_now, fixed_now + timedelta(hours=1)]

    coord.forecast_coordinator.data = {
        "pv_forecast_kw": [0.0, 0.0],
        "consumption_forecast_kw": [0.5, 0.5],
        "current_pv_kw": 0.0,
        "current_dc_pv_kw": 0.0,
        "current_consumption_kw": 0.5,
    }
    hass.states.async_set("sensor.test_price", "0.20")

    monkeypatch.setattr(
        "custom_components.battery_controller.coordinator_optimization.extract_price_forecast_with_timestamps",
        lambda state: (prices, price_times, 60),
    )
    monkeypatch.setattr(
        "custom_components.battery_controller.coordinator_optimization.compute_step_durations_hours",
        lambda *a: [1.0, 1.0],
    )
    monkeypatch.setattr(
        "custom_components.battery_controller.coordinator_optimization.resample_forecast",
        lambda values, src, dst: list(values),
    )
    monkeypatch.setattr(
        "custom_components.battery_controller.coordinator_optimization.dt_util.utcnow",
        lambda: fixed_now,
    )
    monkeypatch.setattr(
        "custom_components.battery_controller.coordinator_optimization.dt_util.now",
        lambda: fixed_now,
    )
    monkeypatch.setattr(coord, "_refresh_battery_config", lambda: None)
    monkeypatch.setattr(
        coord,
        "get_current_battery_state",
        lambda: BatteryState(soc_kwh=5.0, soc_percent=50.0, power_kw=0.0, mode="idle"),
    )
    monkeypatch.setattr(coord, "_get_realtime_grid_w", lambda: 50.0)
    monkeypatch.setattr(coord, "_split_setpoint", lambda kw, _mode="": {"bat1": kw})
    monkeypatch.setattr(coord, "_get_manual_setpoint_w", lambda: -900.0)
    monkeypatch.setattr(coord._price_model, "has_data", lambda: False)
    monkeypatch.setattr(coord._feed_in_price_model, "has_data", lambda: False)

    live_entry = MagicMock()
    live_entry.options = {}
    monkeypatch.setattr(hass.config_entries, "async_get_entry", lambda eid: live_entry)

    fake_result = OptimizationResult(
        power_schedule_kw=[0.0, 0.0],
        mode_schedule=["idle", "idle"],
        soc_schedule_kwh=[5.0, 5.0, 5.0],
        total_cost=0.0,
        baseline_cost=0.0,
        savings=0.0,
        optimal_power_kw=0.0,
        optimal_mode="idle",
        shadow_price_eur_kwh=0.15,
        price_forecast=prices,
        pv_forecast=[0.0, 0.0],
        consumption_forecast=[0.5, 0.5],
    )

    coord.zero_grid_controller = MagicMock()
    coord.zero_grid_controller.get_control_action = MagicMock(
        return_value={
            "target_power_kw": -0.9,
            "target_power_w": -900.0,
            "action_mode": "manual",
            "raw_target_w": -900.0,
            "dp_schedule_w": -900.0,
            "mode": "manual",
        }
    )

    with upatch(
        "custom_components.battery_controller.coordinator_optimization.optimize_battery_schedule",
        return_value=fake_result,
    ):
        data = await coord._run_optimization()

    assert data["optimal_mode"] == "manual"


# ---------------------------------------------------------------------------
# Charge efficiency calibration tests
# ---------------------------------------------------------------------------


def _make_fake_result(
    mode_schedule: list[str],
    soc_schedule_kwh: list[float],
) -> OptimizationResult:
    """Build a minimal OptimizationResult for calibration tests."""
    return OptimizationResult(
        power_schedule_kw=[1.0] * (len(mode_schedule)),
        mode_schedule=mode_schedule,
        soc_schedule_kwh=soc_schedule_kwh,
        total_cost=0.0,
        baseline_cost=0.0,
        savings=0.0,
        optimal_power_kw=1.0,
        optimal_mode=mode_schedule[0] if mode_schedule else "idle",
        shadow_price_eur_kwh=0.10,
        price_forecast=[0.20],
        pv_forecast=[0.0],
        consumption_forecast=[0.5],
    )


def _mark_plan_executed(coord) -> None:
    """Simulate that the previous DP plan was commanded to the controller unchanged."""
    coord._effective_mode = coord._last_result.mode_schedule[0]
    coord._controller_schedule_w = coord._last_result.power_schedule_kw[0] * 1000


def test_calibration_no_sample_without_previous_result(hass):
    """No calibration sample when _last_result is None."""
    coord = _make_coordinator(hass)
    assert coord._charge_eff_correction == 1.0

    battery_state = BatteryState(
        soc_kwh=5.0, soc_percent=50.0, power_kw=0.0, mode="idle"
    )
    coord._update_charge_eff_calibration(battery_state)

    assert coord._charge_eff_correction == 1.0
    assert len(coord._charge_eff_samples) == 0


def test_calibration_no_sample_for_idle_step(hass):
    """No sample collected when previous step was idle."""
    coord = _make_coordinator(hass)
    coord._last_result = _make_fake_result(
        mode_schedule=["idle", "charging"],
        soc_schedule_kwh=[5.0, 5.0, 6.2],
    )

    battery_state = BatteryState(
        soc_kwh=5.0, soc_percent=50.0, power_kw=0.0, mode="idle"
    )
    coord._update_charge_eff_calibration(battery_state)

    assert len(coord._charge_eff_samples) == 0
    assert coord._charge_eff_correction == 1.0


def test_calibration_no_sample_when_planned_delta_too_small(hass):
    """No sample when planned charge delta is below 0.1 kWh threshold."""
    coord = _make_coordinator(hass)
    coord._last_result = _make_fake_result(
        mode_schedule=["charging"],
        soc_schedule_kwh=[5.0, 5.05],  # only 0.05 kWh planned
    )

    battery_state = BatteryState(
        soc_kwh=5.03, soc_percent=50.0, power_kw=1.0, mode="charging"
    )
    coord._update_charge_eff_calibration(battery_state)

    assert len(coord._charge_eff_samples) == 0


def test_calibration_perfect_efficiency_no_correction(hass):
    """When actual delta equals planned delta, correction stays at 1.0."""
    coord = _make_coordinator(hass)
    coord._last_result = _make_fake_result(
        mode_schedule=["charging"],
        soc_schedule_kwh=[5.0, 6.0],  # planned delta = 1.0 kWh
    )
    _mark_plan_executed(coord)

    # Actual SoC reached exactly the planned value
    battery_state = BatteryState(
        soc_kwh=6.0, soc_percent=60.0, power_kw=1.0, mode="charging"
    )
    coord._update_charge_eff_calibration(battery_state)

    assert len(coord._charge_eff_samples) == 1
    assert coord._charge_eff_samples[0] == pytest.approx(1.0)
    assert coord._charge_eff_correction == pytest.approx(1.0)


def test_calibration_waits_until_previous_step_has_elapsed(hass, monkeypatch):
    """Do not sample halfway through the previously planned charging step."""
    coord = _make_coordinator(hass)
    coord._last_result = _make_fake_result(
        mode_schedule=["charging"],
        soc_schedule_kwh=[5.0, 6.0],
    )
    coord.data = {
        "step_start_times_iso": ["2026-04-15T10:00:00+00:00"],
        "step_durations_hours": [1.0],
    }

    monkeypatch.setattr(
        "custom_components.battery_controller.coordinator_optimization.dt_util.utcnow",
        lambda: datetime(2026, 4, 15, 10, 15, tzinfo=timezone.utc),
    )
    _mark_plan_executed(coord)

    battery_state = BatteryState(
        soc_kwh=5.25, soc_percent=52.5, power_kw=1.0, mode="charging"
    )
    coord._update_charge_eff_calibration(battery_state)

    assert len(coord._charge_eff_samples) == 0
    assert coord._charge_eff_correction == 1.0


def test_calibration_samples_after_previous_step_has_elapsed(hass, monkeypatch):
    """Collect a sample once the previous planned charging step has finished."""
    coord = _make_coordinator(hass)
    coord._last_result = _make_fake_result(
        mode_schedule=["charging"],
        soc_schedule_kwh=[5.0, 6.0],
    )
    coord.data = {
        "step_start_times_iso": ["2026-04-15T10:00:00+00:00"],
        "step_durations_hours": [1.0],
    }

    monkeypatch.setattr(
        "custom_components.battery_controller.coordinator_optimization.dt_util.utcnow",
        lambda: datetime(2026, 4, 15, 11, 0, tzinfo=timezone.utc),
    )
    _mark_plan_executed(coord)

    battery_state = BatteryState(
        soc_kwh=6.0, soc_percent=60.0, power_kw=1.0, mode="charging"
    )
    coord._update_charge_eff_calibration(battery_state)

    assert len(coord._charge_eff_samples) == 1
    assert coord._charge_eff_samples[0] == pytest.approx(1.0)
    assert coord._charge_eff_correction == pytest.approx(1.0)


def test_calibration_low_efficiency_updates_correction(hass):
    """When battery only charged to 80% of plan, correction moves below 1."""
    coord = _make_coordinator(hass)
    coord._last_result = _make_fake_result(
        mode_schedule=["charging"],
        soc_schedule_kwh=[4.0, 6.0],  # planned delta = 2.0 kWh
    )
    _mark_plan_executed(coord)

    # Actual delta = 1.6 kWh → ratio = 0.8
    battery_state = BatteryState(
        soc_kwh=5.6, soc_percent=56.0, power_kw=1.0, mode="charging"
    )
    coord._update_charge_eff_calibration(battery_state)

    assert len(coord._charge_eff_samples) == 1
    assert coord._charge_eff_samples[0] == pytest.approx(0.8)
    assert coord._charge_eff_correction == pytest.approx(0.8)


def test_calibration_rolling_average_converges(hass):
    """Multiple samples converge correction to their mean."""
    coord = _make_coordinator(hass)

    for _ in range(4):
        coord._last_result = _make_fake_result(
            mode_schedule=["charging"],
            soc_schedule_kwh=[0.0, 1.0],
        )
        _mark_plan_executed(coord)
        # actual delta = 0.85 kWh → ratio = 0.85
        battery_state = BatteryState(
            soc_kwh=0.85, soc_percent=8.5, power_kw=1.0, mode="charging"
        )
        coord._update_charge_eff_calibration(battery_state)
        # Reset prev_soc for next iteration
        coord._last_result = _make_fake_result(
            mode_schedule=["charging"],
            soc_schedule_kwh=[0.0, 1.0],
        )

    assert coord._charge_eff_correction == pytest.approx(0.85, abs=1e-9)


def test_calibration_floor_clips_extreme_low_ratio(hass):
    """Ratio below 0.5 is clipped to the floor."""
    coord = _make_coordinator(hass)
    coord._last_result = _make_fake_result(
        mode_schedule=["charging"],
        soc_schedule_kwh=[5.0, 7.0],  # planned delta = 2.0 kWh
    )
    _mark_plan_executed(coord)

    # Actual delta = 0.5 kWh → uncipped ratio = 0.25, clipped to 0.5
    battery_state = BatteryState(
        soc_kwh=5.5, soc_percent=55.0, power_kw=1.0, mode="charging"
    )
    coord._update_charge_eff_calibration(battery_state)

    assert coord._charge_eff_samples[0] == pytest.approx(0.5)


def test_calibration_ceiling_clips_extra_charge(hass):
    """Ratio above 1.05 (unexpected extra charge source) is clipped to 1.05."""
    coord = _make_coordinator(hass)
    coord._last_result = _make_fake_result(
        mode_schedule=["charging"],
        soc_schedule_kwh=[5.0, 6.0],  # planned delta = 1.0 kWh
    )
    _mark_plan_executed(coord)

    # Actual delta = 1.5 kWh → unclipped ratio = 1.5, clipped to 1.05
    battery_state = BatteryState(
        soc_kwh=6.5, soc_percent=65.0, power_kw=1.0, mode="charging"
    )
    coord._update_charge_eff_calibration(battery_state)

    assert coord._charge_eff_samples[0] == pytest.approx(1.05)


def test_calibration_not_applied_for_dc_coupled(hass):
    """DC-coupled PV systems are excluded from calibration (passive PV confounds delta).

    The DC-coupled flag lives in the top-level config dict (injected by async_setup_entry
    from PV subentries), not in the battery subentry, so that is where the check reads it.
    """
    from custom_components.battery_controller.const import CONF_PV_DC_COUPLED

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
        # Top-level flag set by async_setup_entry from PV subentries
        CONF_PV_DC_COUPLED: True,
        "battery_subentries": [
            (
                "bat1",
                {
                    CONF_MAX_CHARGE_POWER_KW: 1.2,
                    CONF_MAX_DISCHARGE_POWER_KW: 1.2,
                    CONF_ROUND_TRIP_EFFICIENCY: 0.92,
                    CONF_MIN_SOC_PERCENT: 10.0,
                    CONF_MAX_SOC_PERCENT: 100.0,
                    CONF_BATTERY_SOC_SENSOR: "sensor.test_soc",
                },
            )
        ],
    }
    from custom_components.battery_controller.coordinator_optimization import (
        OptimizationCoordinator as OC,
    )

    coord = OC(hass, weather_coordinator, forecast_coordinator, config)
    coord._last_result = _make_fake_result(
        mode_schedule=["charging"],
        soc_schedule_kwh=[4.0, 6.0],
    )
    _mark_plan_executed(coord)

    battery_state = BatteryState(
        soc_kwh=5.5, soc_percent=55.0, power_kw=1.0, mode="charging"
    )
    coord._update_charge_eff_calibration(battery_state)

    # No sample should have been added for DC-coupled system
    assert len(coord._charge_eff_samples) == 0
    assert coord._charge_eff_correction == 1.0


# ---------------------------------------------------------------------------
# Multi-battery dispatch: SoC-gap triggered concentration
# ---------------------------------------------------------------------------


def _make_two_battery_coord(hass, soc1_kwh: float, soc2_kwh: float):
    """Helper: coordinator with two identical 10 kWh batteries at given SoC levels."""
    from custom_components.battery_controller.battery_model import (
        BatteryConfig,
        BatteryState,
    )

    coord = _make_coordinator(hass)
    cfg = BatteryConfig(
        capacity_kwh=10.0,
        max_charge_power_kw=5.0,
        max_discharge_power_kw=5.0,
        round_trip_efficiency=0.90,
        min_soc_percent=10.0,
        max_soc_percent=90.0,
    )
    coord._individual_battery_configs = [("bat1", cfg), ("bat2", cfg)]
    coord._per_battery_states = {
        "bat1": BatteryState(
            soc_kwh=soc1_kwh, soc_percent=soc1_kwh * 10, power_kw=0.0, mode="idle"
        ),
        "bat2": BatteryState(
            soc_kwh=soc2_kwh, soc_percent=soc2_kwh * 10, power_kw=0.0, mode="idle"
        ),
    }
    return coord


def test_split_setpoint_concentrate_small_charge(hass):
    """Below gap threshold: full charge setpoint goes to lowest-rel_soc battery."""
    # bat1 rel_soc=(5-1)/8=0.5, bat2 rel_soc=(5.2-1)/8=0.525 → gap=0.025 < 0.10
    coord = _make_two_battery_coord(hass, soc1_kwh=5.0, soc2_kwh=5.2)
    result = coord._split_setpoint(0.1, "follow_schedule")  # 100 W charge
    # bat1 has lower rel_soc → gets the setpoint; bat2 stays at 0
    assert result["bat1"] == pytest.approx(0.1, abs=1e-6)
    assert result["bat2"] == pytest.approx(0.0, abs=1e-6)


def test_split_setpoint_concentrate_small_discharge(hass):
    """Below gap threshold: full discharge setpoint goes to highest-rel_soc battery."""
    # bat1 rel_soc=0.5, bat2 rel_soc=0.525 → gap=0.025 < 0.10
    coord = _make_two_battery_coord(hass, soc1_kwh=5.0, soc2_kwh=5.2)
    result = coord._split_setpoint(-0.1, "follow_schedule")  # 100 W discharge
    # bat2 has higher rel_soc → gets the discharge
    assert result["bat2"] == pytest.approx(-0.1, abs=1e-6)
    assert result["bat1"] == pytest.approx(0.0, abs=1e-6)


def test_split_setpoint_proportional_above_gap(hass):
    """Above split threshold: proportional split is used to rebalance SoC."""
    # bat1 rel_soc=(3-1)/8=0.25, bat2 rel_soc=(7-1)/8=0.75 → gap=0.5 >= 0.10
    coord = _make_two_battery_coord(hass, soc1_kwh=3.0, soc2_kwh=7.0)
    result = coord._split_setpoint(2.0, "follow_schedule")
    # Both batteries should receive power
    assert result["bat1"] > 0
    assert result["bat2"] > 0
    assert result["bat1"] + result["bat2"] == pytest.approx(2.0, abs=0.01)
    # bat1 has more headroom (9-3=6) vs bat2 (9-7=2) → bat1 gets more
    assert result["bat1"] > result["bat2"]


def test_split_setpoint_overflow_redistribution(hass):
    """Power overflow above max_charge_power is redistributed to the other battery."""
    from custom_components.battery_controller.battery_model import (
        BatteryConfig,
        BatteryState,
    )

    coord = _make_coordinator(hass)
    cfg_small = BatteryConfig(
        capacity_kwh=5.0,
        max_charge_power_kw=1.0,  # low max
        max_discharge_power_kw=5.0,
        round_trip_efficiency=0.90,
        min_soc_percent=10.0,
        max_soc_percent=90.0,
    )
    cfg_large = BatteryConfig(
        capacity_kwh=5.0,
        max_charge_power_kw=5.0,
        max_discharge_power_kw=5.0,
        round_trip_efficiency=0.90,
        min_soc_percent=10.0,
        max_soc_percent=90.0,
    )
    # rel_soc: bat1=(2.5-0.5)/4=0.5, bat2=(3.0-0.5)/4=0.625 → gap=0.125 >= 0.05 → proportional
    coord._individual_battery_configs = [("bat1", cfg_small), ("bat2", cfg_large)]
    coord._per_battery_states = {
        "bat1": BatteryState(soc_kwh=2.5, soc_percent=50.0, power_kw=0.0, mode="idle"),
        "bat2": BatteryState(soc_kwh=3.0, soc_percent=60.0, power_kw=0.0, mode="idle"),
    }
    result = coord._split_setpoint(4.0, "follow_schedule")
    # bat1 capped at 1 kW, bat2 absorbs remainder → total must equal 4 kW
    assert result["bat1"] == pytest.approx(1.0, abs=0.01)
    assert result["bat1"] + result["bat2"] == pytest.approx(4.0, abs=0.01)


def test_split_setpoint_zero_grid_closest_to_midpoint(hass):
    """Zero-grid: concentrate on battery closest to 50% rel_soc."""
    # bat1 rel_soc=0.5 (exact mid), bat2 rel_soc=0.525 → gap=0.025 < 0.10
    # zero_grid should pick bat1 (closer to 50%)
    coord = _make_two_battery_coord(hass, soc1_kwh=5.0, soc2_kwh=5.2)
    result = coord._split_setpoint(0.1, "zero_grid")
    assert result["bat1"] == pytest.approx(0.1, abs=1e-6)
    assert result["bat2"] == pytest.approx(0.0, abs=1e-6)


def test_split_setpoint_zero_grid_discharge_same_battery(hass):
    """Zero-grid: same battery used for discharge as for charge (no direction switch)."""
    coord = _make_two_battery_coord(hass, soc1_kwh=5.0, soc2_kwh=5.2)
    # First call: charge → bat1 selected (closest to 50%)
    coord._split_setpoint(0.1, "zero_grid")
    active_after_charge = coord._zero_grid_active_battery
    # Second call: discharge → should stay on same battery (hysteresis, gap still < 0.10)
    coord._split_setpoint(-0.1, "zero_grid")
    assert coord._zero_grid_active_battery == active_after_charge


def test_split_setpoint_hysteresis_prevents_switch(hass):
    """Active battery is not replaced unless challenger advantage exceeds hysteresis."""
    coord = _make_two_battery_coord(hass, soc1_kwh=5.0, soc2_kwh=5.2)
    # Initial selection for scheduled charge: bat1 (rel_soc=0.5 < 0.525)
    coord._split_setpoint(0.1, "follow_schedule")
    assert coord._scheduled_active_battery == "bat1"
    # Nudge bat2 slightly lower but advantage still within hysteresis (< 0.05)
    from custom_components.battery_controller.battery_model import BatteryState

    coord._per_battery_states["bat2"] = BatteryState(
        soc_kwh=4.7, soc_percent=47.0, power_kw=0.0, mode="idle"
    )
    # bat2 rel_soc=(4.7-1)/8=0.4625, bat1=0.5 → advantage=0.0375 < 0.05 hysteresis
    # gap=0.0375 < 0.10 → still in concentration mode
    coord._split_setpoint(0.1, "follow_schedule")
    assert coord._scheduled_active_battery == "bat1"  # no switch


def test_split_setpoint_hysteresis_allows_switch(hass):
    """Active battery switches when challenger advantage exceeds hysteresis (gap still < split threshold)."""
    coord = _make_two_battery_coord(hass, soc1_kwh=5.0, soc2_kwh=5.2)
    coord._split_setpoint(0.1, "follow_schedule")
    assert coord._scheduled_active_battery == "bat1"
    # Drop bat2 so advantage = 0.5 - 0.3875 = 0.1125 > 0.05 hysteresis, gap=0.1125 > 0.10 split
    # → need gap in (0.05, 0.10): bat2 at rel_soc such that advantage > 0.05 but gap < 0.10
    # bat2 rel_soc = 0.5 - 0.07 = 0.43 → advantage=0.07 > 0.05, gap=0.07 < 0.10
    from custom_components.battery_controller.battery_model import BatteryState

    # rel_soc=0.43 → soc_kwh = 0.43*8 + 1 = 4.44
    coord._per_battery_states["bat2"] = BatteryState(
        soc_kwh=4.44, soc_percent=44.4, power_kw=0.0, mode="idle"
    )
    coord._split_setpoint(0.1, "follow_schedule")
    assert coord._scheduled_active_battery == "bat2"  # switched


def test_split_setpoint_gap_resets_active_battery(hass):
    """When gap crosses split threshold, active battery state is cleared for fresh selection."""
    coord = _make_two_battery_coord(hass, soc1_kwh=5.0, soc2_kwh=5.2)
    coord._split_setpoint(0.1, "follow_schedule")
    assert coord._scheduled_active_battery == "bat1"
    # Force SoC gap >= 0.10
    from custom_components.battery_controller.battery_model import BatteryState

    coord._per_battery_states["bat2"] = BatteryState(
        soc_kwh=7.5, soc_percent=75.0, power_kw=0.0, mode="idle"
    )
    # bat1 rel_soc=0.5, bat2 rel_soc=(7.5-1)/8=0.8125 → gap=0.3125 >= 0.10
    coord._split_setpoint(0.1, "follow_schedule")
    # Gap crossed threshold → proportional split → active battery reset to None
    assert coord._scheduled_active_battery is None


# ---------------------------------------------------------------------------
# Degradation cost conversion tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_degradation_per_cycle_converted_to_per_kwh_throughput(hass, monkeypatch):
    """Per-cycle degradation cost is divided by 2 x usable kWh (charge + discharge)."""
    from unittest.mock import patch as upatch

    coord = _make_coordinator(hass)
    fixed_now = datetime(2026, 3, 21, 10, 0, 0, tzinfo=timezone.utc)
    coord.forecast_coordinator.data = {
        "pv_forecast_kw": [0.0, 0.0],
        "consumption_forecast_kw": [0.5, 0.5],
        "current_pv_kw": 0.0,
        "current_dc_pv_kw": 0.0,
        "current_consumption_kw": 0.5,
    }
    hass.states.async_set("sensor.test_price", "0.20")
    monkeypatch.setattr(coord, "_refresh_battery_config", lambda: None)
    monkeypatch.setattr(
        coord,
        "get_current_battery_state",
        lambda: BatteryState(soc_kwh=5.0, soc_percent=50.0, power_kw=0.0, mode="idle"),
    )
    monkeypatch.setattr(coord, "_get_realtime_grid_w", lambda: 0.0)
    monkeypatch.setattr(coord, "_split_setpoint", lambda kw, _mode="": {"bat1": kw})
    monkeypatch.setattr(coord._price_model, "has_data", lambda: False)
    monkeypatch.setattr(coord._feed_in_price_model, "has_data", lambda: False)
    monkeypatch.setattr(
        "custom_components.battery_controller.coordinator_optimization.extract_price_forecast_with_timestamps",
        lambda state: ([0.20, 0.22], [fixed_now, fixed_now + timedelta(hours=1)], 60),
    )
    monkeypatch.setattr(
        "custom_components.battery_controller.coordinator_optimization.compute_step_durations_hours",
        lambda *a: [1.0, 1.0],
    )
    monkeypatch.setattr(
        "custom_components.battery_controller.coordinator_optimization.resample_forecast",
        lambda values, src, dst: list(values),
    )
    monkeypatch.setattr(
        "custom_components.battery_controller.coordinator_optimization.dt_util.utcnow",
        lambda: fixed_now,
    )

    live_entry = MagicMock()
    live_entry.options = {"degradation_cost_per_cycle": 0.04}
    monkeypatch.setattr(hass.config_entries, "async_get_entry", lambda eid: live_entry)

    captured = {}

    def fake_optimize(*args):
        captured["degradation_cost_per_kwh"] = args[7]
        return OptimizationResult(
            power_schedule_kw=[0.0, 0.0],
            mode_schedule=["idle", "idle"],
            soc_schedule_kwh=[5.0, 5.0, 5.0],
            total_cost=0.0,
            baseline_cost=0.0,
            savings=0.0,
            optimal_power_kw=0.0,
            optimal_mode="idle",
            shadow_price_eur_kwh=0.15,
            price_forecast=list(args[2]),
            pv_forecast=[0.0, 0.0],
            consumption_forecast=[0.5, 0.5],
        )

    coord.zero_grid_controller = MagicMock()
    coord.zero_grid_controller.get_control_action = MagicMock(
        return_value={
            "target_power_kw": 0.0,
            "target_power_w": 0.0,
            "action_mode": "idle",
            "raw_target_w": 0.0,
            "dp_schedule_w": 0.0,
            "mode": "idle",
        }
    )

    with upatch(
        "custom_components.battery_controller.coordinator_optimization.optimize_battery_schedule",
        side_effect=fake_optimize,
    ):
        await coord._run_optimization()

    # Default battery: 10 kWh, 10-100% SoC -> usable 9 kWh.
    # One full cycle = 18 kWh throughput (charge + discharge), so
    # 0.04 EUR/cycle -> 0.04 / 18 EUR/kWh.
    usable_kwh = coord.battery_config.max_soc_kwh - coord.battery_config.min_soc_kwh
    assert usable_kwh == pytest.approx(9.0)
    assert captured["degradation_cost_per_kwh"] == pytest.approx(0.04 / (2 * 9.0))


# ---------------------------------------------------------------------------
# Sensor unit handling
# ---------------------------------------------------------------------------


def test_grid_power_kw_sensor_converted(hass):
    """kW grid sensors are converted to W."""
    coord = _make_coordinator(hass)
    coord._power_consumption_sensors = ["sensor.grid_kw"]
    hass.states.async_set("sensor.grid_kw", "1.5", {"unit_of_measurement": "kW"})
    assert coord._get_realtime_grid_w() == pytest.approx(1500.0)


def test_grid_power_unknown_unit_skipped(hass, caplog):
    """A grid sensor with an unrecognized unit is skipped, not misread as W."""
    coord = _make_coordinator(hass)
    coord._power_consumption_sensors = ["sensor.grid_mw", "sensor.grid_w"]
    hass.states.async_set("sensor.grid_mw", "2.0", {"unit_of_measurement": "MW"})
    hass.states.async_set("sensor.grid_w", "300", {"unit_of_measurement": "W"})
    assert coord._get_realtime_grid_w() == pytest.approx(300.0)
    assert "unexpected unit 'MW'" in caplog.text
    # Warning is emitted only once per sensor
    caplog.clear()
    coord._get_realtime_grid_w()
    assert "unexpected unit" not in caplog.text


def test_soc_sensor_in_wh_converted(hass):
    """A Wh SoC sensor is converted to kWh."""
    coord = _make_coordinator(hass)
    hass.states.async_set("sensor.test_soc", "5000", {"unit_of_measurement": "Wh"})
    sid, subentry = coord._battery_subentries[0]
    cfg = dict(coord._individual_battery_configs)[sid]
    state = coord._read_battery_state(subentry, cfg)
    assert state.soc_kwh == pytest.approx(5.0)
    assert state.soc_percent == pytest.approx(50.0)


def test_concentrate_redistributes_when_winner_full(hass):
    """A full concentration winner must not drop the charge setpoint."""
    # bat1 full (9.0 = max SoC), bat2 nearly full; gap below split threshold.
    coord = _make_two_battery_coord(hass, soc1_kwh=9.0, soc2_kwh=8.7)
    # Selection hysteresis keeps the previously active (now full) battery.
    coord._zero_grid_active_battery = "bat1"
    result = coord._split_setpoint(1.0, MODE_ZERO_GRID)
    # bat1 has no headroom; the setpoint must go to bat2 instead of vanishing.
    assert result["bat1"] == pytest.approx(0.0, abs=1e-6)
    assert result["bat2"] == pytest.approx(1.0, abs=1e-6)


def test_concentrate_redistributes_when_winner_empty(hass):
    """An empty concentration winner must not drop the discharge setpoint."""
    # bat1 empty (1.0 = min SoC), bat2 slightly above; gap below split threshold.
    coord = _make_two_battery_coord(hass, soc1_kwh=1.0, soc2_kwh=1.3)
    coord._zero_grid_active_battery = "bat1"
    result = coord._split_setpoint(-1.0, MODE_ZERO_GRID)
    assert result["bat1"] == pytest.approx(0.0, abs=1e-6)
    assert result["bat2"] == pytest.approx(-1.0, abs=1e-6)


@pytest.mark.asyncio
async def test_price_change_from_zero_uses_absolute_threshold(hass, monkeypatch):
    """A price moving away from exactly 0 must still trigger re-optimization."""
    coordinator = _make_coordinator(hass)

    period = datetime(2026, 3, 21, 10, 0, 0, tzinfo=timezone.utc)
    coordinator._last_price = 0.0  # free hour
    coordinator._last_period_start = period

    monkeypatch.setattr(
        "custom_components.battery_controller.coordinator_optimization.extract_price_forecast_with_timestamps",
        lambda state: ([0.05], [period], 60),
    )
    monkeypatch.setattr(
        "custom_components.battery_controller.coordinator_optimization.dt_util.utcnow",
        lambda: period + timedelta(minutes=5),
    )

    refresh_called = []

    async def fake_refresh():
        refresh_called.append(True)

    monkeypatch.setattr(coordinator, "async_request_refresh", fake_refresh)

    old_mock = MagicMock()
    old_mock.state = "0.0"
    new_mock = MagicMock()
    new_mock.state = "0.05"
    event = MagicMock()
    event.data = {"old_state": old_mock, "new_state": new_mock}

    coordinator._handle_price_change(event)
    await hass.async_block_till_done()

    assert refresh_called, "absolute threshold should trigger from a zero price"
    assert coordinator._last_price == pytest.approx(0.05)


@pytest.mark.asyncio
async def test_price_change_from_zero_small_change_updates_price(hass, monkeypatch):
    """Tiny changes from 0 do not trigger, but _last_price must still update."""
    coordinator = _make_coordinator(hass)

    period = datetime(2026, 3, 21, 10, 0, 0, tzinfo=timezone.utc)
    coordinator._last_price = 0.0
    coordinator._last_period_start = period

    monkeypatch.setattr(
        "custom_components.battery_controller.coordinator_optimization.extract_price_forecast_with_timestamps",
        lambda state: ([0.001], [period], 60),
    )
    monkeypatch.setattr(
        "custom_components.battery_controller.coordinator_optimization.dt_util.utcnow",
        lambda: period + timedelta(minutes=5),
    )

    refresh_called = []

    async def fake_refresh():
        refresh_called.append(True)

    monkeypatch.setattr(coordinator, "async_request_refresh", fake_refresh)

    old_mock = MagicMock()
    old_mock.state = "0.0"
    new_mock = MagicMock()
    new_mock.state = "0.001"
    event = MagicMock()
    event.data = {"old_state": old_mock, "new_state": new_mock}

    coordinator._handle_price_change(event)
    await hass.async_block_till_done()

    assert not refresh_called
    # Previously _last_price stayed stuck at 0 because the whole branch was
    # skipped for a zero price.
    assert coordinator._last_price == pytest.approx(0.001)


# ---------------------------------------------------------------------------
# Calibration gating: only sample when the DP plan was actually executed
# ---------------------------------------------------------------------------


def test_calibration_no_sample_when_plan_overridden_by_zero_grid(hass):
    """No sample when hybrid/zero_grid resolved the planned charge to zero_grid."""
    coord = _make_coordinator(hass)
    coord._last_result = _make_fake_result(
        mode_schedule=["charging"],
        soc_schedule_kwh=[4.0, 6.0],
    )
    # Hybrid resolved the planned charge to zero_grid: schedule not executed.
    coord._effective_mode = "zero_grid"
    coord._controller_schedule_w = 0.0

    battery_state = BatteryState(
        soc_kwh=4.1, soc_percent=41.0, power_kw=0.0, mode="idle"
    )
    coord._update_charge_eff_calibration(battery_state)

    assert len(coord._charge_eff_samples) == 0
    assert coord._charge_eff_correction == 1.0


def test_calibration_no_sample_when_commitment_locked_other_power(hass):
    """No sample when the commitment filter locked a different power."""
    coord = _make_coordinator(hass)
    coord._last_result = _make_fake_result(
        mode_schedule=["charging"],
        soc_schedule_kwh=[4.0, 6.0],
    )
    # Same direction but commitment filter locked 0.4 kW instead of planned 1.0 kW.
    coord._effective_mode = "charging"
    coord._controller_schedule_w = 400.0

    battery_state = BatteryState(
        soc_kwh=4.4, soc_percent=44.0, power_kw=0.4, mode="charging"
    )
    coord._update_charge_eff_calibration(battery_state)

    assert len(coord._charge_eff_samples) == 0
    assert coord._charge_eff_correction == 1.0


def test_discharge_calibration_no_sample_when_plan_overridden(hass):
    """Discharge calibration is gated on the executed plan as well."""
    coord = _make_coordinator(hass)
    coord._last_result = _make_fake_result(
        mode_schedule=["discharging"],
        soc_schedule_kwh=[6.0, 4.0],
    )
    coord._effective_mode = "zero_grid"
    coord._controller_schedule_w = 0.0

    battery_state = BatteryState(
        soc_kwh=5.9, soc_percent=59.0, power_kw=0.0, mode="idle"
    )
    coord._update_discharge_eff_calibration(battery_state)

    assert len(coord._discharge_eff_samples) == 0
    assert coord._discharge_eff_correction == 1.0


def test_discharge_calibration_samples_when_plan_executed(hass):
    """Discharge calibration samples normally when the plan was executed."""
    coord = _make_coordinator(hass)
    coord._last_result = _make_fake_result(
        mode_schedule=["discharging"],
        soc_schedule_kwh=[6.0, 4.0],  # planned delta = 2.0 kWh down
    )
    _mark_plan_executed(coord)

    # Actual delta = 1.6 kWh down → ratio = 0.8
    battery_state = BatteryState(
        soc_kwh=4.4, soc_percent=44.0, power_kw=-1.0, mode="discharging"
    )
    coord._update_discharge_eff_calibration(battery_state)

    assert len(coord._discharge_eff_samples) == 1
    assert coord._discharge_eff_samples[0] == pytest.approx(0.8)
    assert coord._discharge_eff_correction == pytest.approx(0.8)


# Feed-in interval handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_feed_in_forecast_resampled_from_native_interval(hass, monkeypatch):
    """Hourly feed-in prices are expanded to the 15-min grid price interval."""
    from unittest.mock import patch as upatch

    coord = _make_coordinator(hass)
    fixed_now = datetime(2026, 3, 21, 10, 0, 0, tzinfo=timezone.utc)
    coord.config[CONF_FEED_IN_PRICE_SENSOR] = "sensor.feed_in"
    coord.forecast_coordinator.data = {
        "pv_forecast_kw": [0.0],
        "consumption_forecast_kw": [0.5],
        "current_pv_kw": 0.0,
        "current_dc_pv_kw": 0.0,
        "current_consumption_kw": 0.5,
    }
    hass.states.async_set("sensor.test_price", "0.20")
    # Feed-in sensor publishes two HOURLY values via the generic forecast attr
    # (no timestamps -> detected interval = 60 min).
    hass.states.async_set("sensor.feed_in", "0.10", {"forecast": [0.10, 0.20]})
    monkeypatch.setattr(coord, "_refresh_battery_config", lambda: None)
    monkeypatch.setattr(
        coord,
        "get_current_battery_state",
        lambda: BatteryState(soc_kwh=5.0, soc_percent=50.0, power_kw=0.0, mode="idle"),
    )
    monkeypatch.setattr(coord, "_get_realtime_grid_w", lambda: 0.0)
    monkeypatch.setattr(coord, "_split_setpoint", lambda kw, _mode="": {"bat1": kw})
    monkeypatch.setattr(coord._price_model, "has_data", lambda: False)
    monkeypatch.setattr(coord._feed_in_price_model, "has_data", lambda: False)
    # Grid price sensor publishes 15-min prices: 4 steps = 1 hour
    monkeypatch.setattr(
        "custom_components.battery_controller.coordinator_optimization.extract_price_forecast_with_timestamps",
        lambda state: (
            [0.20, 0.21, 0.22, 0.23],
            [fixed_now + timedelta(minutes=15 * i) for i in range(4)],
            15,
        ),
    )
    monkeypatch.setattr(
        "custom_components.battery_controller.coordinator_optimization.compute_step_durations_hours",
        lambda *a: [0.25] * 4,
    )
    monkeypatch.setattr(
        "custom_components.battery_controller.coordinator_optimization.dt_util.utcnow",
        lambda: fixed_now,
    )

    live_entry = MagicMock()
    live_entry.options = {}
    monkeypatch.setattr(hass.config_entries, "async_get_entry", lambda eid: live_entry)

    captured = {}

    def fake_optimize(*args):
        captured["feed_in"] = args[3]
        return OptimizationResult(
            power_schedule_kw=[0.0] * 4,
            mode_schedule=["idle"] * 4,
            soc_schedule_kwh=[5.0] * 5,
            total_cost=0.0,
            baseline_cost=0.0,
            savings=0.0,
            optimal_power_kw=0.0,
            optimal_mode="idle",
            shadow_price_eur_kwh=0.15,
            price_forecast=list(args[2]),
            pv_forecast=[0.0] * 4,
            consumption_forecast=[0.5] * 4,
        )

    coord.zero_grid_controller = MagicMock()
    coord.zero_grid_controller.get_control_action = MagicMock(
        return_value={
            "target_power_kw": 0.0,
            "target_power_w": 0.0,
            "action_mode": "idle",
            "raw_target_w": 0.0,
            "dp_schedule_w": 0.0,
            "mode": "idle",
        }
    )

    with upatch(
        "custom_components.battery_controller.coordinator_optimization.optimize_battery_schedule",
        side_effect=fake_optimize,
    ):
        await coord._run_optimization()

    # The first hourly feed-in value (0.10) must cover the first four 15-min
    # steps. Without interval-aware resampling the second hourly value (0.20)
    # would incorrectly appear at step 1.
    assert captured["feed_in"][:4] == pytest.approx([0.10, 0.10, 0.10, 0.10])


# ---------------------------------------------------------------------------

# Hybrid mode: planned charging vs PV export surplus
# ---------------------------------------------------------------------------


async def _run_hybrid_charge_case(hass, monkeypatch, grid_w: float):
    """Run one hybrid optimization with a 3 kW planned charge and given grid power."""
    from unittest.mock import patch as upatch

    coord = _make_coordinator(hass)
    coord.control_mode = MODE_HYBRID
    fixed_now = datetime(2026, 3, 21, 10, 0, 0, tzinfo=timezone.utc)
    coord.forecast_coordinator.data = {
        "pv_forecast_kw": [0.5, 0.5],
        "consumption_forecast_kw": [0.3, 0.3],
        "current_pv_kw": 0.5,
        "current_dc_pv_kw": 0.0,
        "current_consumption_kw": 0.3,
    }
    hass.states.async_set("sensor.test_price", "0.10")
    monkeypatch.setattr(coord, "_refresh_battery_config", lambda: None)
    monkeypatch.setattr(
        coord,
        "get_current_battery_state",
        lambda: BatteryState(soc_kwh=5.0, soc_percent=50.0, power_kw=0.0, mode="idle"),
    )
    monkeypatch.setattr(coord, "_get_realtime_grid_w", lambda: grid_w)
    monkeypatch.setattr(coord, "_split_setpoint", lambda kw, _mode="": {"bat1": kw})
    monkeypatch.setattr(coord._price_model, "has_data", lambda: False)
    monkeypatch.setattr(coord._feed_in_price_model, "has_data", lambda: False)
    monkeypatch.setattr(
        "custom_components.battery_controller.coordinator_optimization.extract_price_forecast_with_timestamps",
        lambda state: ([0.10, 0.12], [fixed_now, fixed_now + timedelta(hours=1)], 60),
    )
    monkeypatch.setattr(
        "custom_components.battery_controller.coordinator_optimization.compute_step_durations_hours",
        lambda *a: [1.0, 1.0],
    )
    monkeypatch.setattr(
        "custom_components.battery_controller.coordinator_optimization.resample_forecast",
        lambda values, src, dst: list(values),
    )
    monkeypatch.setattr(
        "custom_components.battery_controller.coordinator_optimization.dt_util.utcnow",
        lambda: fixed_now,
    )
    live_entry = MagicMock()
    live_entry.options = {}
    monkeypatch.setattr(hass.config_entries, "async_get_entry", lambda eid: live_entry)

    fake_result = OptimizationResult(
        power_schedule_kw=[3.0, 0.0],
        mode_schedule=["charging", "idle"],
        soc_schedule_kwh=[5.0, 7.8, 7.8],
        total_cost=0.0,
        baseline_cost=0.0,
        savings=0.0,
        optimal_power_kw=3.0,
        optimal_mode="charging",
        shadow_price_eur_kwh=0.15,
        price_forecast=[0.10, 0.12],
        pv_forecast=[0.5, 0.5],
        consumption_forecast=[0.3, 0.3],
    )
    coord.zero_grid_controller = MagicMock()
    coord.zero_grid_controller.get_control_action = MagicMock(
        return_value={
            "target_power_kw": 0.0,
            "target_power_w": 0.0,
            "action_mode": "idle",
            "raw_target_w": 0.0,
            "dp_schedule_w": 0.0,
            "mode": "idle",
        }
    )
    with upatch(
        "custom_components.battery_controller.coordinator_optimization.optimize_battery_schedule",
        return_value=fake_result,
    ):
        data = await coord._run_optimization()
    return data


@pytest.mark.asyncio
async def test_hybrid_small_surplus_follows_charge_schedule(hass, monkeypatch):
    """A 0.2 kW export surplus must not downgrade a 3 kW planned grid charge."""
    data = await _run_hybrid_charge_case(hass, monkeypatch, grid_w=-200.0)
    assert data["optimal_mode"] == "charging"
    assert data["optimal_power_kw"] == pytest.approx(3.0)


@pytest.mark.asyncio
async def test_hybrid_large_surplus_uses_zero_grid(hass, monkeypatch):
    """When the surplus covers the planned charge, zero_grid follows the surplus."""
    data = await _run_hybrid_charge_case(hass, monkeypatch, grid_w=-3500.0)
    assert data["optimal_mode"] == "zero_grid"


async def _run_hybrid_mode_sequence(
    hass,
    monkeypatch,
    fake_result: OptimizationResult,
    grid_sequence: list[float],
    deadband_w: float | None = None,
    control_mode: str = MODE_HYBRID,
) -> list[str]:
    """Run hybrid optimization repeatedly on one coordinator, varying grid power.

    Used to verify hysteresis: unlike `_run_hybrid_charge_case`, which builds a
    fresh coordinator per call (so it can't observe state carried between
    realtime-update ticks), this reuses the same coordinator so the
    `_last_hybrid_idle_decision` / `_last_hybrid_charge_decision` hysteresis
    state persists across the calls in grid_sequence.
    """
    from unittest.mock import patch as upatch

    coord = _make_coordinator(hass)
    coord.control_mode = control_mode
    fixed_now = datetime(2026, 3, 21, 10, 0, 0, tzinfo=timezone.utc)
    coord.forecast_coordinator.data = {
        "pv_forecast_kw": [0.5, 0.5],
        "consumption_forecast_kw": [0.3, 0.3],
        "current_pv_kw": 0.5,
        "current_dc_pv_kw": 0.0,
        "current_consumption_kw": 0.3,
    }
    hass.states.async_set("sensor.test_price", "0.10")
    monkeypatch.setattr(coord, "_refresh_battery_config", lambda: None)
    monkeypatch.setattr(
        coord,
        "get_current_battery_state",
        lambda: BatteryState(soc_kwh=5.0, soc_percent=50.0, power_kw=0.0, mode="idle"),
    )
    monkeypatch.setattr(coord, "_split_setpoint", lambda kw, _mode="": {"bat1": kw})
    monkeypatch.setattr(coord._price_model, "has_data", lambda: False)
    monkeypatch.setattr(coord._feed_in_price_model, "has_data", lambda: False)
    monkeypatch.setattr(
        "custom_components.battery_controller.coordinator_optimization.extract_price_forecast_with_timestamps",
        lambda state: ([0.10, 0.12], [fixed_now, fixed_now + timedelta(hours=1)], 60),
    )
    monkeypatch.setattr(
        "custom_components.battery_controller.coordinator_optimization.compute_step_durations_hours",
        lambda *a: [1.0, 1.0],
    )
    monkeypatch.setattr(
        "custom_components.battery_controller.coordinator_optimization.resample_forecast",
        lambda values, src, dst: list(values),
    )
    monkeypatch.setattr(
        "custom_components.battery_controller.coordinator_optimization.dt_util.utcnow",
        lambda: fixed_now,
    )
    live_entry = MagicMock()
    live_entry.options = (
        {} if deadband_w is None else {CONF_ZERO_GRID_DEADBAND_W: deadband_w}
    )
    monkeypatch.setattr(hass.config_entries, "async_get_entry", lambda eid: live_entry)

    coord.zero_grid_controller = MagicMock()
    coord.zero_grid_controller.get_control_action = MagicMock(
        return_value={
            "target_power_kw": 0.0,
            "target_power_w": 0.0,
            "action_mode": "idle",
            "raw_target_w": 0.0,
            "dp_schedule_w": 0.0,
            "mode": "idle",
        }
    )

    modes: list[str] = []
    with upatch(
        "custom_components.battery_controller.coordinator_optimization.optimize_battery_schedule",
        return_value=fake_result,
    ):
        for grid_w in grid_sequence:
            monkeypatch.setattr(coord, "_get_realtime_grid_w", lambda gw=grid_w: gw)
            data = await coord._run_optimization()
            modes.append(data["optimal_mode"])
    return modes


@pytest.mark.asyncio
async def test_hybrid_charge_surplus_hysteresis_prevents_flicker(hass, monkeypatch):
    """PV surplus hovering around the 80% coverage threshold must not flip the
    effective mode every realtime tick.

    Planned charge is 3 kW, so the coverage threshold is 2400 W. The first
    tick has a solid 2600 W surplus (enters zero_grid). The second tick drops
    to 2350 W: below the raw 2400 W threshold, but within the ±5% hysteresis
    band, so it must stay in zero_grid rather than flicker back to charging.
    """
    fake_result = OptimizationResult(
        power_schedule_kw=[3.0, 0.0],
        mode_schedule=["charging", "idle"],
        soc_schedule_kwh=[5.0, 7.8, 7.8],
        total_cost=0.0,
        baseline_cost=0.0,
        savings=0.0,
        optimal_power_kw=3.0,
        optimal_mode="charging",
        shadow_price_eur_kwh=0.15,
        price_forecast=[0.10, 0.12],
        pv_forecast=[0.5, 0.5],
        consumption_forecast=[0.3, 0.3],
    )
    modes = await _run_hybrid_mode_sequence(
        hass, monkeypatch, fake_result, grid_sequence=[-2600.0, -2350.0]
    )
    assert modes == ["zero_grid", "zero_grid"]


@pytest.mark.asyncio
async def test_hybrid_idle_zero_grid_hysteresis_prevents_flicker(hass, monkeypatch):
    """Grid power hovering near 0 W must not flip idle/zero_grid every tick.

    The optimizer wants idle (preserve capacity for an upcoming discharge).
    The first tick has a solid PV surplus (-100 W, below the default -50 W
    zero-grid-deadband band) so it enters zero_grid. The second tick is a
    small positive import (20 W) — within the ±50 W deadband — so it must
    stay in zero_grid rather than flicker back to idle.
    """
    fake_result = OptimizationResult(
        power_schedule_kw=[0.0, 2.0],
        mode_schedule=["idle", "discharging"],
        soc_schedule_kwh=[5.0, 5.0, 3.0],
        total_cost=0.0,
        baseline_cost=0.0,
        savings=0.0,
        optimal_power_kw=0.0,
        optimal_mode="idle",
        shadow_price_eur_kwh=0.15,
        price_forecast=[0.10, 0.12],
        pv_forecast=[0.5, 0.5],
        consumption_forecast=[0.3, 0.3],
    )
    modes = await _run_hybrid_mode_sequence(
        hass, monkeypatch, fake_result, grid_sequence=[-100.0, 20.0]
    )
    assert modes == ["zero_grid", "zero_grid"]


@pytest.mark.asyncio
async def test_hybrid_idle_zero_grid_deadband_is_configurable(hass, monkeypatch):
    """The hysteresis band width follows the user's zero_grid_deadband_w setting.

    Same grid sequence as the default-band test above, but with the deadband
    turned down to 10 W. The 20 W import on the second tick now exceeds the
    (smaller) band, so the mode must flip back to idle — proving the band
    width actually comes from the live config value, not a fixed constant.
    """
    fake_result = OptimizationResult(
        power_schedule_kw=[0.0, 2.0],
        mode_schedule=["idle", "discharging"],
        soc_schedule_kwh=[5.0, 5.0, 3.0],
        total_cost=0.0,
        baseline_cost=0.0,
        savings=0.0,
        optimal_power_kw=0.0,
        optimal_mode="idle",
        shadow_price_eur_kwh=0.15,
        price_forecast=[0.10, 0.12],
        pv_forecast=[0.5, 0.5],
        consumption_forecast=[0.3, 0.3],
    )
    modes = await _run_hybrid_mode_sequence(
        hass,
        monkeypatch,
        fake_result,
        grid_sequence=[-100.0, 20.0],
        deadband_w=10.0,
    )
    assert modes == ["zero_grid", "idle"]


# ---------------------------------------------------------------------------
# Hybrid+ mode: PV-surplus capture gated on the price forecast (shadow price)
# ---------------------------------------------------------------------------


def _idle_plan_result(shadow_price: float) -> OptimizationResult:
    """Optimizer result planning idle now and charging later (cheap surplus)."""
    return OptimizationResult(
        power_schedule_kw=[0.0, 3.0],
        mode_schedule=["idle", "charging"],
        soc_schedule_kwh=[5.0, 5.0, 7.8],
        total_cost=0.0,
        baseline_cost=0.0,
        savings=0.0,
        optimal_power_kw=0.0,
        optimal_mode="idle",
        shadow_price_eur_kwh=shadow_price,
        price_forecast=[0.10, 0.12],
        pv_forecast=[0.5, 0.5],
        consumption_forecast=[0.3, 0.3],
    )


@pytest.mark.asyncio
async def test_hybrid_plus_low_shadow_price_exports_surplus(hass, monkeypatch):
    """Hybrid+ exports PV surplus when the battery can be filled cheaper later.

    The optimizer plans idle now and charging later; its shadow price of
    0.02 EUR/kWh (× sqrt(0.92) ≈ 0.019) is well below the 0.07 EUR/kWh fixed
    feed-in price, so exporting the current surplus beats storing it and the
    mode must stay idle instead of upgrading to zero_grid.
    """
    modes = await _run_hybrid_mode_sequence(
        hass,
        monkeypatch,
        _idle_plan_result(shadow_price=0.02),
        grid_sequence=[-2000.0],
        control_mode=MODE_HYBRID_PLUS,
    )
    assert modes == ["idle"]


@pytest.mark.asyncio
async def test_hybrid_plus_high_shadow_price_captures_surplus(hass, monkeypatch):
    """Hybrid+ still captures surplus when stored energy is worth more than feed-in.

    Shadow price 0.15 EUR/kWh (× sqrt(0.92) ≈ 0.144) exceeds the 0.07 EUR/kWh
    feed-in price, so storing the surplus beats exporting it — same behaviour
    as plain hybrid.
    """
    modes = await _run_hybrid_mode_sequence(
        hass,
        monkeypatch,
        _idle_plan_result(shadow_price=0.15),
        grid_sequence=[-2000.0],
        control_mode=MODE_HYBRID_PLUS,
    )
    assert modes == ["zero_grid"]


@pytest.mark.asyncio
async def test_hybrid_captures_surplus_regardless_of_shadow_price(hass, monkeypatch):
    """Plain hybrid is unchanged: surplus is captured even at a low shadow price."""
    modes = await _run_hybrid_mode_sequence(
        hass,
        monkeypatch,
        _idle_plan_result(shadow_price=0.02),
        grid_sequence=[-2000.0],
        control_mode=MODE_HYBRID,
    )
    assert modes == ["zero_grid"]


@pytest.mark.asyncio
async def test_hybrid_plus_self_consumption_not_gated(hass, monkeypatch):
    """The hybrid+ gate only affects surplus capture, not self-consumption.

    With the grid importing (no PV surplus) and no upcoming discharge, hybrid+
    must still resolve idle to zero_grid for self-consumption, even though the
    low shadow price blocks surplus capture.
    """
    modes = await _run_hybrid_mode_sequence(
        hass,
        monkeypatch,
        _idle_plan_result(shadow_price=0.02),
        grid_sequence=[500.0],
        control_mode=MODE_HYBRID_PLUS,
    )
    assert modes == ["zero_grid"]


def test_hybrid_plus_capture_decision_hysteresis(hass):
    """The capture decision applies a ±5% band around the feed-in threshold.

    Test coordinator: RTE 0.92 → sqrt(RTE) ≈ 0.9592; feed-in 0.07 EUR/kWh.
    Capture threshold while capturing: 0.07 × 0.95 = 0.0665; while exporting:
    0.07 × 1.05 = 0.0735 (both on the stored value λ × sqrt(RTE)).
    """
    coord = _make_coordinator(hass)
    # Initial state is capturing: keeps capturing down to the ×0.95 band edge
    assert coord._hybrid_plus_should_capture_surplus(0.10, 0.07) is True
    # Stored value 0.0652 < 0.0665 → flips to exporting
    assert coord._hybrid_plus_should_capture_surplus(0.068, 0.07) is False
    # Stored value 0.0691 is above the raw threshold but below ×1.05 → stays
    # exporting (this is the hysteresis: without it the decision would flip)
    assert coord._hybrid_plus_should_capture_surplus(0.072, 0.07) is False
    # Stored value 0.0748 ≥ 0.0735 → flips back to capturing
    assert coord._hybrid_plus_should_capture_surplus(0.078, 0.07) is True


def test_hybrid_plus_negative_feed_in_always_captures(hass):
    """With zero/negative feed-in, exporting earns nothing: always capture."""
    coord = _make_coordinator(hass)
    assert coord._hybrid_plus_should_capture_surplus(0.0, -0.05) is True
    assert coord._hybrid_plus_should_capture_surplus(0.0, 0.0) is True


def test_resolve_controller_mode_hybrid_plus_blocks_surplus_upgrade(hass):
    """The realtime idle→zero_grid upgrade respects the hybrid+ capture block."""
    coord = _make_coordinator(hass)
    coord._control_mode = MODE_HYBRID_PLUS
    coord._power_consumption_sensors = ["sensor.power"]

    coord._hybrid_plus_capture_blocked = True
    assert coord._resolve_controller_mode("idle", -500.0) == "idle"

    coord._hybrid_plus_capture_blocked = False
    assert coord._resolve_controller_mode("idle", -500.0) == "zero_grid"

    # Plain hybrid ignores the flag entirely
    coord._control_mode = MODE_HYBRID
    coord._hybrid_plus_capture_blocked = True
    assert coord._resolve_controller_mode("idle", -500.0) == "zero_grid"

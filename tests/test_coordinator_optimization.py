"""Tests for OptimizationCoordinator commitment behavior and scheduling."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from custom_components.battery_controller.battery_model import BatteryState
from custom_components.battery_controller.const import (
    CONF_BATTERY_SOC_SENSOR,
    CONF_CONTROL_MODE,
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
    MODE_FOLLOW_SCHEDULE,
    MODE_HYBRID,
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
        lambda combined_setpoint_kw: {"bat1": combined_setpoint_kw},
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
        "async_get_known_entry",
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

"""Test harness for driving OptimizationCoordinator._run_optimization.

A full optimization run needs a lot of the world stubbed: a clock, a price
sensor and its extraction, step durations, forecast data, a battery state, a
grid reading, two price models, a live config entry, an optimizer result and a
zero-grid controller. Written out per test that is roughly ninety lines of
scaffolding around one or two assertions, and the thing under test disappears
into it.

This builds all of it with defaults that let a run complete, and exposes each
piece as an attribute to change before calling :meth:`run`. A test then shows
only what it is actually about.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import MagicMock, patch

from custom_components.battery_controller.battery_model import BatteryState
from custom_components.battery_controller.optimizer import OptimizationResult

DEFAULT_NOW = datetime(2026, 3, 21, 10, 0, 0, tzinfo=timezone.utc)

# Positional signature of optimize_battery_schedule, so captured arguments can
# be read by name instead of by index.
_OPTIMIZER_ARGS = (
    "battery_config",
    "current_soc_kwh",
    "prices",
    "feed_in",
    "pv",
    "consumption",
    "durations",
    "degradation_cost_per_kwh",
    "min_price_spread",
    "pv_dc",
    "charge_eff_curve_override",
    "discharge_eff_curve_override",
)


class OptimizationRunHarness:
    """Drive one _run_optimization() with everything around it stubbed."""

    def __init__(self, hass, monkeypatch, coordinator) -> None:
        self.hass = hass
        self.monkeypatch = monkeypatch
        self.coord = coordinator

        self.now = DEFAULT_NOW
        self.prices: list[float] = [0.20, 0.22]
        self.price_interval = 60
        self.price_start_times: list[datetime] | None = None
        self.step_durations: list[float] | None = None
        self.extract_price = None  # optional callable(state) for per-entity control
        # "unavailable" here drives the price-sensor-missing paths.
        self.price_sensor_state: str | None = None

        self.pv: list[float] | None = None
        self.consumption: list[float] | None = None
        self.forecast_interval: int | None = None
        self.forecast_start: datetime | None = None
        self.forecast_data: dict[str, Any] | None = None

        self.battery_state = BatteryState(
            soc_kwh=5.0, soc_percent=50.0, power_kw=0.0, mode="idle"
        )
        self.grid_w: float | None = 0.0
        self.entry_options: dict[str, Any] = {}
        self.price_model_has_data = False
        self.feed_in_model_has_data = False
        self.passthrough_resample = True
        # Leave compute_step_durations_hours alone when a test is about
        # the real step windows (a shortened first step, for instance).
        self.real_step_durations = False

        self.result: OptimizationResult | None = None
        self.control_action: dict[str, Any] | None = None
        self.captured: dict[str, Any] = {}

    # -- defaults derived from whatever the test did set ------------------

    def _times(self) -> list[datetime]:
        if self.price_start_times is not None:
            return self.price_start_times
        return [
            self.now + timedelta(minutes=self.price_interval * i)
            for i in range(len(self.prices))
        ]

    def _durations(self) -> list[float]:
        if self.step_durations is not None:
            return self.step_durations
        return [self.price_interval / 60.0] * len(self.prices)

    def _forecast(self) -> dict[str, Any]:
        if self.forecast_data is not None:
            return self.forecast_data
        n = len(self.prices)
        data: dict[str, Any] = {
            "pv_forecast_kw": self.pv if self.pv is not None else [0.0] * n,
            "consumption_forecast_kw": (
                self.consumption if self.consumption is not None else [0.5] * n
            ),
            "current_pv_kw": 0.0,
            "current_dc_pv_kw": 0.0,
            "current_consumption_kw": 0.5,
        }
        if self.forecast_interval is not None:
            data["forecast_interval_minutes"] = self.forecast_interval
        if self.forecast_start is not None:
            data["forecast_start_utc"] = self.forecast_start
        return data

    def _result(self) -> OptimizationResult:
        if self.result is not None:
            return self.result
        n = len(self.prices)
        return OptimizationResult(
            power_schedule_kw=[0.0] * n,
            mode_schedule=["idle"] * n,
            soc_schedule_kwh=[5.0] * (n + 1),
            total_cost=0.0,
            baseline_cost=0.0,
            savings=0.0,
            optimal_power_kw=0.0,
            optimal_mode="idle",
            shadow_price_eur_kwh=0.15,
            price_forecast=list(self.prices),
            pv_forecast=[0.0] * n,
            consumption_forecast=[0.5] * n,
        )

    def _control_action(self) -> dict[str, Any]:
        base = {
            "target_power_kw": 0.0,
            "target_power_w": 0.0,
            "action_mode": "idle",
            "raw_target_w": 0.0,
            "dp_schedule_w": 0.0,
            "mode": "idle",
        }
        base.update(self.control_action or {})
        return base

    # -- the run ----------------------------------------------------------

    async def run(self) -> dict[str, Any]:
        """Apply every stub and return the coordinator data dict."""
        mp, coord, module = (
            self.monkeypatch,
            self.coord,
            "custom_components.battery_controller.coordinator_optimization",
        )

        coord.forecast_coordinator.data = self._forecast()
        self.hass.states.async_set(
            "sensor.test_price",
            self.price_sensor_state
            if self.price_sensor_state is not None
            else str(self.prices[0]),
        )

        mp.setattr(f"{module}.dt_util.utcnow", lambda: self.now)
        mp.setattr(f"{module}.dt_util.now", lambda: self.now)
        mp.setattr(
            f"{module}.extract_price_forecast_with_timestamps",
            self.extract_price
            or (lambda state: (self.prices, self._times(), self.price_interval)),
        )
        if not self.real_step_durations:
            mp.setattr(
                f"{module}.compute_step_durations_hours", lambda *a: self._durations()
            )
        if self.passthrough_resample:
            mp.setattr(f"{module}.resample_forecast", lambda values, src, dst: list(values))

        mp.setattr(coord, "_refresh_battery_config", lambda: None)
        mp.setattr(coord, "get_current_battery_state", lambda: self.battery_state)
        mp.setattr(coord, "_get_realtime_grid_w", lambda: self.grid_w)
        mp.setattr(coord, "_split_setpoint", lambda kw, _mode="": {"bat1": kw})
        mp.setattr(coord._price_model, "has_data", lambda: self.price_model_has_data)
        mp.setattr(
            coord._feed_in_price_model, "has_data", lambda: self.feed_in_model_has_data
        )

        live_entry = MagicMock()
        live_entry.options = self.entry_options
        mp.setattr(self.hass.config_entries, "async_get_entry", lambda eid: live_entry)

        coord.zero_grid_controller = MagicMock()
        coord.zero_grid_controller.get_control_action = MagicMock(
            return_value=self._control_action()
        )

        result = self._result()

        def _fake_optimize(*args):
            self.captured.update(dict(zip(_OPTIMIZER_ARGS, args)))
            return result

        with patch(f"{module}.optimize_battery_schedule", side_effect=_fake_optimize):
            return await coord._run_optimization()

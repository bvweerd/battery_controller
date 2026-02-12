"""Tests for zero_grid_controller.py."""

import pytest

from custom_components.battery_controller.battery_model import BatteryConfig
from custom_components.battery_controller.zero_grid_controller import (
    ZeroGridController,
    ZeroGridControllerConfig,
    create_zero_grid_controller,
)


@pytest.fixture
def battery_config():
    return BatteryConfig(
        capacity_kwh=10.0,
        max_charge_power_kw=5.0,
        max_discharge_power_kw=5.0,
        min_soc_percent=10.0,
        max_soc_percent=90.0,
    )


@pytest.fixture
def controller_config():
    return ZeroGridControllerConfig(
        max_charge_w=5000.0,
        max_discharge_w=5000.0,
        deadband_w=50.0,
    )


@pytest.fixture
def controller(controller_config, battery_config):
    return ZeroGridController(controller_config, battery_config)


class TestZeroGridMode:
    """Tests for zero_grid control mode."""

    def test_importing_triggers_discharge(self, controller):
        """When importing from grid, discharge battery."""
        target = controller.calculate_battery_setpoint(
            current_grid_w=1000,  # Importing 1 kW
            current_soc_kwh=5.0,
            dp_schedule_w=0,
            mode="zero_grid",
        )
        # Should discharge (negative) to compensate
        assert target < 0
        assert target == pytest.approx(-1000, abs=10)

    def test_exporting_triggers_charge(self, controller):
        """When exporting to grid, charge battery."""
        target = controller.calculate_battery_setpoint(
            current_grid_w=-2000,  # Exporting 2 kW
            current_soc_kwh=5.0,
            dp_schedule_w=0,
            mode="zero_grid",
        )
        assert target > 0
        assert target == pytest.approx(2000, abs=10)

    def test_zero_grid_at_zero(self, controller):
        """No grid exchange -> no battery action."""
        target = controller.calculate_battery_setpoint(
            current_grid_w=0,
            current_soc_kwh=5.0,
            dp_schedule_w=0,
            mode="zero_grid",
        )
        assert target == 0.0

    def test_clamp_to_max_discharge(self, controller):
        """Large import should be clamped to max discharge."""
        target = controller.calculate_battery_setpoint(
            current_grid_w=8000,  # 8 kW import
            current_soc_kwh=5.0,
            dp_schedule_w=0,
            mode="zero_grid",
        )
        assert target == pytest.approx(-5000)  # Max discharge

    def test_soc_limit_prevents_discharge(self, controller):
        """At min SoC, don't discharge further."""
        target = controller.calculate_battery_setpoint(
            current_grid_w=2000,
            current_soc_kwh=1.0,  # At min SoC (10% of 10 kWh)
            dp_schedule_w=0,
            mode="zero_grid",
        )
        assert target == 0.0  # Can't discharge

    def test_soc_limit_prevents_charge(self, controller):
        """At max SoC, don't charge further."""
        target = controller.calculate_battery_setpoint(
            current_grid_w=-2000,
            current_soc_kwh=9.0,  # At max SoC (90% of 10 kWh)
            dp_schedule_w=0,
            mode="zero_grid",
        )
        assert target == 0.0  # Can't charge


class TestFollowScheduleMode:
    """Tests for follow_schedule control mode."""

    def test_follows_dp_schedule(self, controller):
        """Should follow DP schedule exactly."""
        target = controller.calculate_battery_setpoint(
            current_grid_w=1000,  # Ignored in follow_schedule
            current_soc_kwh=5.0,
            dp_schedule_w=2000,  # DP says charge at 2kW
            mode="follow_schedule",
        )
        assert target == pytest.approx(2000)

    def test_follows_discharge_schedule(self, controller):
        target = controller.calculate_battery_setpoint(
            current_grid_w=0,
            current_soc_kwh=5.0,
            dp_schedule_w=-3000,  # DP says discharge at 3kW
            mode="follow_schedule",
        )
        assert target == pytest.approx(-3000)

    def test_schedule_clamped_to_max(self, controller):
        target = controller.calculate_battery_setpoint(
            current_grid_w=0,
            current_soc_kwh=5.0,
            dp_schedule_w=8000,  # Beyond max charge
            mode="follow_schedule",
        )
        assert target == pytest.approx(5000)


class TestUnknownMode:
    """Unknown mode falls through to manual (returns 0)."""

    def test_unknown_mode_returns_zero(self, controller):
        target = controller.calculate_battery_setpoint(
            current_grid_w=1000,
            current_soc_kwh=5.0,
            dp_schedule_w=2000,
            mode="unknown_mode",
        )
        assert target == 0.0


class TestIdleMode:
    """Tests for idle control mode (preserve battery for peak pricing)."""

    def test_idle_no_discharge_when_importing(self, controller):
        """When grid is importing, idle mode should NOT discharge battery."""
        target = controller.calculate_battery_setpoint(
            current_grid_w=1000,  # Importing 1 kW from grid
            current_soc_kwh=5.0,
            dp_schedule_w=0,
            mode="idle",
        )
        assert target == 0.0  # Preserve battery capacity

    def test_idle_does_nothing_with_pv_surplus(self, controller):
        """Idle mode does nothing, even with PV surplus."""
        target = controller.calculate_battery_setpoint(
            current_grid_w=-2000,  # Exporting 2 kW (PV surplus)
            current_soc_kwh=5.0,
            dp_schedule_w=0,
            mode="idle",
        )
        assert target == 0.0  # True idle: no charge, no discharge

    def test_idle_zero_grid_no_action(self, controller):
        """When grid is balanced, idle mode does nothing."""
        target = controller.calculate_battery_setpoint(
            current_grid_w=0,
            current_soc_kwh=5.0,
            dp_schedule_w=0,
            mode="idle",
        )
        assert target == 0.0

    def test_idle_ignores_dp_schedule(self, controller):
        """Idle mode ignores DP schedule completely."""
        target = controller.calculate_battery_setpoint(
            current_grid_w=1000,
            current_soc_kwh=5.0,
            dp_schedule_w=-5000,  # DP says discharge hard
            mode="idle",
        )
        assert target == 0.0  # Idle overrides everything


class TestManualMode:
    """Tests for manual control mode."""

    def test_manual_returns_zero(self, controller):
        target = controller.calculate_battery_setpoint(
            current_grid_w=5000,
            current_soc_kwh=5.0,
            dp_schedule_w=3000,
            mode="manual",
        )
        assert target == 0.0


class TestDeadband:
    """Tests for deadband hysteresis."""

    def test_within_deadband_no_change(self, controller):
        # Set previous target
        controller._last_target_w = 1000
        result = controller.apply_deadband(target_w=1020)
        assert result == 1000  # Within 50W deadband

    def test_exceeds_deadband_changes(self, controller):
        # Set previous target
        controller._last_target_w = 1000
        result = controller.apply_deadband(target_w=1100)
        assert result == 1100  # Exceeds 50W deadband

    def test_first_call_no_deadband(self, controller):
        # First call with _last_target_w = 0
        result = controller.apply_deadband(target_w=1000)
        assert result == 1000  # No previous target, apply new target


class TestGetControlAction:
    """Tests for get_control_action."""

    def test_returns_dict(self, controller):
        action = controller.get_control_action(
            current_grid_w=500,
            current_soc_kwh=5.0,
            current_battery_w=0,
            dp_schedule_w=1000,
            mode="hybrid",
        )
        assert isinstance(action, dict)
        assert "target_power_w" in action
        assert "target_power_kw" in action
        assert "action_mode" in action
        assert "soc_percent" in action

    def test_action_mode_charging(self, controller):
        action = controller.get_control_action(
            current_grid_w=-2000,
            current_soc_kwh=5.0,
            current_battery_w=0,
            dp_schedule_w=0,
            mode="zero_grid",
        )
        assert action["action_mode"] == "charging"

    def test_action_mode_discharging(self, controller):
        action = controller.get_control_action(
            current_grid_w=2000,
            current_soc_kwh=5.0,
            current_battery_w=0,
            dp_schedule_w=0,
            mode="zero_grid",
        )
        assert action["action_mode"] == "discharging"

    def test_soc_percent_calculation(self, controller):
        action = controller.get_control_action(
            current_grid_w=0,
            current_soc_kwh=5.0,
            current_battery_w=0,
            dp_schedule_w=0,
            mode="manual",
        )
        assert action["soc_percent"] == pytest.approx(50.0)


class TestCreateZeroGridController:
    """Tests for create_zero_grid_controller factory."""

    def test_creates_controller(self, battery_config):
        config = {
            "zero_grid_deadband_w": 100.0,
            "zero_grid_response_time_s": 10.0,
            "zero_grid_priority": "zero_grid",
        }
        controller = create_zero_grid_controller(config, battery_config)
        assert isinstance(controller, ZeroGridController)
        assert controller.config.deadband_w == 100.0

    def test_uses_defaults(self, battery_config):
        controller = create_zero_grid_controller({}, battery_config)
        assert isinstance(controller, ZeroGridController)
        assert controller.config.deadband_w == 50.0

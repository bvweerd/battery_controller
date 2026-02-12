"""Real-time zero-grid controller for the Battery Controller integration."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .battery_model import BatteryConfig
from .helpers import clamp

_LOGGER = logging.getLogger(__name__)


@dataclass
class ZeroGridControllerConfig:
    """Configuration for the zero-grid controller."""

    max_charge_w: float = 5000.0
    max_discharge_w: float = 5000.0
    deadband_w: float = 50.0  # Hysteresis to prevent oscillation
    response_time_s: float = 5.0  # Update interval
    priority: str = "schedule"  # 'zero_grid' or 'schedule' when in conflict


class ZeroGridController:
    """Real-time controller for zero-grid operation.

    This controller runs every second/minute to minimize grid exchange
    by using the battery as a buffer. Works together with the DP optimizer.

    Modes:
    - zero_grid: Pure zero-grid operation, compensate grid fully
    - follow_schedule: Follow DP optimization schedule exactly
    - hybrid: Follow schedule but correct real-time deviations
    """

    def __init__(
        self,
        config: ZeroGridControllerConfig,
        battery_config: BatteryConfig,
    ):
        """Initialize the zero-grid controller."""
        self.config = config
        self.battery_config = battery_config
        self._last_target_w = 0.0
        self._setpoint_w = 0.0  # Target grid power (0 = zero-grid)

    def calculate_battery_setpoint(
        self,
        current_grid_w: float,
        current_soc_kwh: float,
        dp_schedule_w: float,
        mode: str,
    ) -> float:
        """Calculate the desired battery power setpoint.

        Args:
            current_grid_w: Current grid power in W (positive = import)
            current_soc_kwh: Current battery SoC in kWh
            dp_schedule_w: What the DP optimizer recommends in W
            mode: Control mode ('zero_grid', 'follow_schedule', 'hybrid')

        Returns:
            Desired battery power in W (positive = charge, negative = discharge)
        """
        if mode == "zero_grid":
            return self._calculate_zero_grid(current_grid_w, current_soc_kwh)
        elif mode == "idle":
            return self._calculate_idle(current_grid_w, current_soc_kwh)
        elif mode == "follow_schedule":
            return self._calculate_follow_schedule(dp_schedule_w, current_soc_kwh)
        else:
            # Manual mode - return 0 (no automatic control)
            return 0.0

    def _calculate_zero_grid(
        self,
        current_grid_w: float,
        current_soc_kwh: float,
    ) -> float:
        """Pure zero-grid mode: compensate grid exchange fully.

        Args:
            current_grid_w: Current grid power in W (positive = import)
            current_soc_kwh: Current battery SoC in kWh

        Returns:
            Battery power setpoint in W
        """
        # Use previous target rather than actual battery power to avoid oscillation.
        # Formula: target = last_target - grid_error
        # This is equivalent to target = -(load - pv) = pv - load, but
        # remains stable because it does not include the actual battery power
        # reading (which would cancel itself out each cycle via the grid meter).
        target_battery_w = self._last_target_w - current_grid_w

        # Apply battery limits
        target_battery_w = clamp(
            target_battery_w,
            -self.config.max_discharge_w,
            self.config.max_charge_w,
        )

        # Apply SoC limits
        target_battery_w = self._apply_soc_limits(target_battery_w, current_soc_kwh)

        return target_battery_w

    def _calculate_idle(
        self,
        current_grid_w: float,
        current_soc_kwh: float,
    ) -> float:
        """Idle mode: preserve battery capacity completely.

        Used when the optimizer wants to preserve battery for upcoming
        expensive periods. Does nothing - no charge, no discharge.
        The optimizer already accounts for PV in its planning; if
        significant PV surplus exists it recommends 'charging' not 'idle'.

        Returns:
            Battery power setpoint in W (always 0)
        """
        return 0.0

    def _calculate_follow_schedule(
        self,
        dp_schedule_w: float,
        current_soc_kwh: float,
    ) -> float:
        """Follow DP schedule exactly.

        Args:
            dp_schedule_w: DP optimizer recommendation in W
            current_soc_kwh: Current battery SoC in kWh

        Returns:
            Battery power setpoint in W
        """
        target_battery_w = dp_schedule_w

        # Apply battery limits
        target_battery_w = clamp(
            target_battery_w,
            -self.config.max_discharge_w,
            self.config.max_charge_w,
        )

        # Apply SoC limits
        target_battery_w = self._apply_soc_limits(target_battery_w, current_soc_kwh)

        return target_battery_w

    def _apply_soc_limits(
        self,
        target_w: float,
        current_soc_kwh: float,
    ) -> float:
        """Apply SoC limits to the target power.

        Args:
            target_w: Desired power in W
            current_soc_kwh: Current battery SoC in kWh

        Returns:
            Adjusted power respecting SoC limits
        """
        min_soc_kwh = self.battery_config.min_soc_kwh
        max_soc_kwh = self.battery_config.max_soc_kwh

        if current_soc_kwh <= min_soc_kwh and target_w < 0:
            # Can't discharge below min SoC
            return 0.0

        if current_soc_kwh >= max_soc_kwh and target_w > 0:
            # Can't charge above max SoC
            return 0.0

        return target_w

    def apply_deadband(
        self,
        target_w: float,
    ) -> float:
        """Apply deadband to prevent oscillation.

        Only change the setpoint if the difference exceeds the deadband.
        Compares with the previous target, not current battery power,
        to avoid oscillation caused by the battery responding to commands.

        Args:
            target_w: New target power in W

        Returns:
            Adjusted target respecting deadband
        """
        if abs(target_w - self._last_target_w) < self.config.deadband_w:
            return self._last_target_w
        return target_w

    def get_control_action(
        self,
        current_grid_w: float,
        current_soc_kwh: float,
        current_battery_w: float,
        dp_schedule_w: float,
        mode: str,
    ) -> dict[str, Any]:
        """Get the control action with all relevant information.

        Args:
            current_grid_w: Current grid power in W
            current_soc_kwh: Current battery SoC in kWh
            current_battery_w: Current battery power in W
            dp_schedule_w: DP optimizer recommendation in W
            mode: Control mode

        Returns:
            Dict with control action and metadata
        """
        # Calculate raw target
        raw_target_w = self.calculate_battery_setpoint(
            current_grid_w,
            current_soc_kwh,
            dp_schedule_w,
            mode,
        )

        # Apply deadband
        final_target_w = self.apply_deadband(raw_target_w)

        # Update last target for next deadband calculation
        self._last_target_w = final_target_w

        # Determine action mode
        if final_target_w > 50:
            action_mode = "charging"
        elif final_target_w < -50:
            action_mode = "discharging"
        else:
            action_mode = "idle"

        return {
            "target_power_w": final_target_w,
            "target_power_kw": final_target_w / 1000,
            "raw_target_w": raw_target_w,
            "current_grid_w": current_grid_w,
            "current_battery_w": current_battery_w,
            "dp_schedule_w": dp_schedule_w,
            "mode": mode,
            "action_mode": action_mode,
            "soc_kwh": current_soc_kwh,
            "soc_percent": (current_soc_kwh / self.battery_config.capacity_kwh) * 100,
        }


def create_zero_grid_controller(
    config: dict[str, Any],
    battery_config: BatteryConfig,
) -> ZeroGridController:
    """Create a ZeroGridController from configuration.

    Args:
        config: Home Assistant configuration dict
        battery_config: Battery configuration

    Returns:
        Configured ZeroGridController
    """
    from .const import (
        CONF_ZERO_GRID_DEADBAND_W,
        CONF_ZERO_GRID_RESPONSE_TIME_S,
        CONF_ZERO_GRID_PRIORITY,
        DEFAULT_ZERO_GRID_DEADBAND_W,
        DEFAULT_ZERO_GRID_RESPONSE_TIME_S,
        DEFAULT_ZERO_GRID_PRIORITY,
    )

    controller_config = ZeroGridControllerConfig(
        max_charge_w=battery_config.max_charge_power_kw * 1000,
        max_discharge_w=battery_config.max_discharge_power_kw * 1000,
        deadband_w=float(
            config.get(CONF_ZERO_GRID_DEADBAND_W, DEFAULT_ZERO_GRID_DEADBAND_W)
        ),
        response_time_s=float(
            config.get(
                CONF_ZERO_GRID_RESPONSE_TIME_S, DEFAULT_ZERO_GRID_RESPONSE_TIME_S
            )
        ),
        priority=config.get(CONF_ZERO_GRID_PRIORITY, DEFAULT_ZERO_GRID_PRIORITY),
    )

    return ZeroGridController(controller_config, battery_config)

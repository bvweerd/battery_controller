"""Real-time zero-grid controller for the Battery Controller integration."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .battery_model import BatteryConfig
from .const import (
    ACTION_CHARGING,
    ACTION_DISCHARGING,
    ACTION_IDLE,
    ZERO_GRID_LOOP_GAIN,
)
from .helpers import clamp

_LOGGER = logging.getLogger(__name__)


@dataclass
class ZeroGridControllerConfig:
    """Configuration for the zero-grid controller.

    Power limits are not stored here: the controller clamps setpoints with the
    SoC-dependent limits from BatteryConfig (max_charge_at_soc /
    max_discharge_at_soc), which are the authoritative source.
    """

    deadband_w: float = 50.0  # Hysteresis to prevent oscillation
    response_time_s: float = 5.0  # Update interval
    # Integrator gain; must stay below 1. See ZERO_GRID_LOOP_GAIN in const.py
    # for why, and for the settling times behind this value.
    loop_gain: float = ZERO_GRID_LOOP_GAIN


class ZeroGridController:
    """Real-time controller for zero-grid operation.

    This controller runs every second/minute to minimize grid exchange
    by using the battery as a buffer. Works together with the DP optimizer.

    Modes accepted by calculate_battery_setpoint:
    - zero_grid: Pure zero-grid operation, compensate grid fully
    - idle: Hold the battery still
    - follow_schedule: Follow the DP optimization schedule exactly
    - manual: Follow the user's setpoint, with SoC and power limits

    The user-facing control modes (hybrid, hybrid+) are NOT among them: the
    coordinator resolves those into one of the four above per step, before
    calling this class.
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

    @property
    def last_target_w(self) -> float:
        """Return the internal setpoint memory in W (positive = charge)."""
        return self._last_target_w

    def reset_setpoint(self, value_w: float = 0.0) -> None:
        """Force the internal setpoint memory to a specific value.

        Used on mode changes and by the coordinator's stale-sensor fail-safe to
        keep the integrator state consistent when a computed setpoint is
        rejected.
        """
        self._last_target_w = value_w

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
            mode: One of 'zero_grid', 'idle', 'follow_schedule', 'manual'.
                The user-facing 'hybrid' / 'hybrid_plus' modes are resolved by
                the coordinator before they reach this method.

        Returns:
            Desired battery power in W (positive = charge, negative = discharge)
        """
        if mode == "zero_grid":
            return self._calculate_zero_grid(current_grid_w, current_soc_kwh)
        elif mode == ACTION_IDLE:
            return self._calculate_idle(current_grid_w, current_soc_kwh)
        elif mode in ("follow_schedule", "manual"):
            # Manual mode: follow the user-supplied setpoint, same SoC/power limits
            return self._calculate_follow_schedule(dp_schedule_w, current_soc_kwh)
        # Unknown mode — safe fallback: no battery action. Warn rather than fail
        # silently: an unresolved mode (e.g. 'hybrid' passed straight through)
        # would otherwise look like a deliberate decision to hold the battery.
        _LOGGER.warning(
            "Unknown zero-grid controller mode '%s'; holding the battery at 0 W. "
            "Expected one of: zero_grid, idle, follow_schedule, manual",
            mode,
        )
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
        # Formula: target = last_target - gain x grid_error
        # This converges on target = -(load - pv) = pv - load, but does not
        # include the actual battery power reading (which would cancel itself
        # out each cycle via the grid meter).
        #
        # The gain has to stay below 1: at exactly 1 the loop only settles when
        # the meter already reflects the previous tick's setpoint, and one tick
        # of delay turns it into a permanent six-tick oscillation. See
        # ZERO_GRID_LOOP_GAIN.
        target_battery_w = self._last_target_w - self.config.loop_gain * current_grid_w

        # Apply battery limits (SoC-dependent: e.g. BMS absorption near full/empty)
        target_battery_w = clamp(
            target_battery_w,
            -self.battery_config.max_discharge_at_soc(current_soc_kwh) * 1000,
            self.battery_config.max_charge_at_soc(current_soc_kwh) * 1000,
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

        # Apply battery limits (SoC-dependent: e.g. BMS absorption near full/empty)
        target_battery_w = clamp(
            target_battery_w,
            -self.battery_config.max_discharge_at_soc(current_soc_kwh) * 1000,
            self.battery_config.max_charge_at_soc(current_soc_kwh) * 1000,
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

        # Determine action mode: use deadband as the idle threshold
        if final_target_w > self.config.deadband_w:
            action_mode = ACTION_CHARGING
        elif final_target_w < -self.config.deadband_w:
            action_mode = ACTION_DISCHARGING
        else:
            action_mode = ACTION_IDLE

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
            "soc_percent": (
                (current_soc_kwh / self.battery_config.capacity_kwh) * 100
                if self.battery_config.capacity_kwh > 0
                else 0.0
            ),
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
        DEFAULT_ZERO_GRID_DEADBAND_W,
        DEFAULT_ZERO_GRID_RESPONSE_TIME_S,
    )

    controller_config = ZeroGridControllerConfig(
        deadband_w=float(
            config.get(CONF_ZERO_GRID_DEADBAND_W, DEFAULT_ZERO_GRID_DEADBAND_W)
        ),
        response_time_s=float(
            config.get(
                CONF_ZERO_GRID_RESPONSE_TIME_S, DEFAULT_ZERO_GRID_RESPONSE_TIME_S
            )
        ),
    )

    return ZeroGridController(controller_config, battery_config)

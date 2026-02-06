"""Optimization algorithms for the Heating Curve Optimizer integration."""

from __future__ import annotations

import logging
import math

from .const import (
    DEFAULT_COP_AT_35,
    DEFAULT_K_FACTOR,
    DEFAULT_OUTDOOR_TEMP_COEFFICIENT,
    DEFAULT_THERMAL_STORAGE_EFFICIENCY,
)
from .helpers import calculate_defrost_factor, calculate_supply_temperature

_LOGGER = logging.getLogger(__name__)


def _pad_forecast(data: list[float] | None, length: int, default: float) -> list[float]:
    """Pad forecast data to the required length with a default value."""
    if not data:
        return [default] * length
    if len(data) >= length:
        return list(data[:length])
    # Pad with the last value or default
    last_value = data[-1] if data else default
    return list(data) + [last_value] * (length - len(data))


def optimize_offsets(
    demand: list[float],
    prices: list[float],
    *,
    base_temp: float = 35.0,
    k_factor: float = DEFAULT_K_FACTOR,
    cop_compensation_factor: float = 1.0,
    buffer: float = 0.0,
    water_min: float = 28.0,
    water_max: float = 45.0,
    outdoor_temps: list[float] | None = None,
    humidity_forecast: list[float] | None = None,
    outdoor_temp_coefficient: float = DEFAULT_OUTDOOR_TEMP_COEFFICIENT,
    time_base: int = 60,
    outdoor_min: float = -20.0,
    outdoor_max: float = 15.0,
    max_buffer_debt: float = 5.0,
    current_offset: int = 0,
    offset_delta_t: int = 10,
) -> tuple[list[int], list[float]]:
    r"""Return cost optimized offsets for the given demand and prices.

    The ``demand`` list contains the net heat demand per hour.  The
    algorithm uses the total energy over the complete horizon and
    distributes that energy over the hours with a dynamic-programming
    approach.  Offsets are restricted to ``\-4`` .. ``+4`` and may
    change by 1°C per 10 minutes (so up to 6°C per hour at time_base=60).
    The optional ``buffer`` parameter represents the current heat surplus
    (positive) or deficit (negative) and the returned buffer evolution
    shows how this value changes with the chosen offsets.  After the
    planning window the buffer will be close to zero.

    The ``max_buffer_debt`` parameter (default 5.0 kWh) allows the buffer
    to go negative (heat debt), enabling cost optimization by reducing
    heating during expensive hours and compensating during cheaper hours.

    The ``current_offset`` parameter (default 0) specifies the current
    offset value, used to constrain the first step's offset change.

    The ``offset_delta_t`` parameter (default 10) specifies the number of
    minutes per 1°C offset change. Lower values allow faster changes.
    At time_base=60 and offset_delta_t=10: max 6°C change per step.
    At time_base=60 and offset_delta_t=60: max 1°C change per step.
    """
    _LOGGER.debug(
        "Optimizing offsets demand=%s prices=%s base=%s k=%s comp=%s buffer=%s outdoor_temps=%s humidity=%s",
        demand,
        prices,
        base_temp,
        k_factor,
        cop_compensation_factor,
        buffer,
        outdoor_temps,
        humidity_forecast,
    )
    horizon = min(len(demand), len(prices))
    if horizon == 0:
        return [], []

    # Calculate max offset change per step based on offset_delta_t
    # offset_delta_t = minutes per 1°C change
    # At time_base=60 and offset_delta_t=10: max_change = 60/10 = 6°C per step
    # At time_base=60 and offset_delta_t=60: max_change = 60/60 = 1°C per step
    max_offset_change = max(1, time_base // max(1, offset_delta_t))

    _LOGGER.debug(
        "Offset change constraint: max %d°C per %d-min step "
        "(delta_t=%d min/°C, current_offset=%d)",
        max_offset_change,
        time_base,
        offset_delta_t,
        current_offset,
    )

    # Prepare outdoor temperature and humidity data for defrost modeling
    outdoor_temps_data = _pad_forecast(outdoor_temps, horizon, 5.0)
    humidity_data = _pad_forecast(humidity_forecast, horizon, 80.0)

    # Calculate defrost factors for each time step
    defrost_factors = [
        calculate_defrost_factor(outdoor_temps_data[t], humidity_data[t])
        for t in range(horizon)
    ]

    # Calculate base temperature for each forecast step based on outdoor temperature
    base_temps = [
        calculate_supply_temperature(
            outdoor_temps_data[t],
            water_min=water_min,
            water_max=water_max,
            outdoor_min=outdoor_min,
            outdoor_max=outdoor_max,
        )
        for t in range(horizon)
    ]

    def _calculate_cop(offset: int, time_step: int) -> float:
        """Calculate COP for given offset and time step, including outdoor temp and defrost effects."""
        supply_temp = base_temps[time_step] + offset
        outdoor_temp = outdoor_temps_data[time_step]

        # COP formula: base + outdoor_effect - supply_temp_effect
        cop_base = (
            DEFAULT_COP_AT_35
            + outdoor_temp_coefficient * outdoor_temp
            - k_factor * (supply_temp - 35)
        ) * cop_compensation_factor

        # Apply defrost factor
        cop_adjusted = cop_base * defrost_factors[time_step]

        return max(0.5, cop_adjusted)  # Ensure COP doesn't go below 0.5

    # Check which offsets are allowed - must respect water_min/max for all forecast steps
    # An offset is allowed only if it keeps supply temp within bounds for ALL time steps
    allowed_offsets = []
    for o in range(-4, 5):
        if all(water_min <= base_temps[t] + o <= water_max for t in range(horizon)):
            allowed_offsets.append(o)
    if not allowed_offsets:
        return [0 for _ in range(horizon)], [buffer for _ in range(horizon)]

    # Calculate step duration in hours for buffer energy calculation
    step_hours = time_base / 60.0

    # dynamic programming table storing (cost, prev_offset, prev_sum, buffer_energy)
    # State: (time_step, offset) -> {cumulative_sum: (cost, prev_offset, prev_sum, buffer_kwh)}
    dp: list[dict[int, dict[int, tuple[float, int | None, int | None, float]]]] = [
        {} for _ in range(horizon)
    ]

    # First step: constrain offset change from current_offset
    for off in allowed_offsets:
        # Check if offset is reachable from current_offset
        if abs(off - current_offset) > max_offset_change:
            continue

        cop = _calculate_cop(off, 0)
        # Cost = (thermal_demand / COP) * time * price = electrical_energy * price
        # Only pay for positive demand (heating), not for solar charging
        effective_demand = max(float(demand[0]), 0.0)
        cost = (
            effective_demand * step_hours * prices[0] / cop
            if cop > 0
            else effective_demand * step_hours * prices[0] * 10
        )
        # Calculate initial buffer energy
        # Two mechanisms:
        # 1. Active heating with offset: buffer += offset * demand * efficiency
        # 2. Passive solar charging (demand < 0): buffer += -demand (direct charging)
        if demand[0] >= 0:
            # Active heating - offset determines buffer change
            buffer_kwh = (
                buffer
                + off * demand[0] * DEFAULT_THERMAL_STORAGE_EFFICIENCY * step_hours
            )
        else:
            # Solar gain exceeds heat loss - passive charging
            # Buffer increases by the excess solar energy
            buffer_kwh = buffer + abs(demand[0]) * step_hours
        # Allow negative buffer (heat debt) up to max_buffer_debt
        if buffer_kwh >= -max_buffer_debt:
            dp[0][off] = {off: (cost, None, None, buffer_kwh)}

    for t in range(1, horizon):
        for off in allowed_offsets:
            cop = _calculate_cop(off, t)
            # Cost = (thermal_demand / COP) * time * price = electrical_energy * price
            # Only pay for positive demand (heating), not for solar charging
            effective_demand = max(float(demand[t]), 0.0)
            step_cost = (
                effective_demand * step_hours * prices[t] / cop
                if cop > 0
                else effective_demand * step_hours * prices[t] * 10
            )
            for prev_off, sums in dp[t - 1].items():
                if abs(off - prev_off) <= max_offset_change:
                    for prev_sum, (prev_cost, _, _, prev_buffer_kwh) in sums.items():
                        new_sum = prev_sum + off
                        total = prev_cost + step_cost
                        # Calculate new buffer energy
                        # Two mechanisms:
                        # 1. Active heating: buffer += offset * demand * efficiency
                        # 2. Solar charging (demand < 0): buffer += -demand
                        if demand[t] >= 0:
                            # Active heating - offset determines buffer change
                            buffer_kwh = (
                                prev_buffer_kwh
                                + off
                                * demand[t]
                                * DEFAULT_THERMAL_STORAGE_EFFICIENCY
                                * step_hours
                            )
                        else:
                            # Solar gain exceeds heat loss - passive charging
                            buffer_kwh = prev_buffer_kwh + abs(demand[t]) * step_hours
                        # Allow negative buffer (heat debt) up to max_buffer_debt
                        if buffer_kwh >= -max_buffer_debt:
                            dp[t].setdefault(off, {})
                            cur = dp[t][off].get(new_sum)
                            if cur is None or total < cur[0]:
                                dp[t][off][new_sum] = (
                                    total,
                                    prev_off,
                                    prev_sum,
                                    buffer_kwh,
                                )

    if not dp[horizon - 1]:
        return [0 for _ in range(horizon)], [buffer for _ in range(horizon)]

    # Select best solution: minimize (cost + penalty_for_nonzero_buffer)
    # Prefer solutions that return buffer close to zero at end of planning horizon
    best_off: int | None = None
    best_sum: int | None = None
    best_cost = math.inf
    buffer_penalty_weight = (
        0.01  # Small penalty to prefer buffer→0 without dominating cost
    )

    for off, sums in dp[horizon - 1].items():
        for sum_off, (cost, _, _, final_buffer) in sums.items():
            # Penalize non-zero final buffer (prefer to return to 0)
            buffer_penalty = buffer_penalty_weight * abs(final_buffer)
            total_objective = cost + buffer_penalty

            if total_objective < best_cost:
                best_cost = total_objective
                best_off = off
                best_sum = sum_off

    assert best_off is not None and best_sum is not None

    result = [0] * horizon
    last_off = best_off
    last_sum = best_sum
    result[-1] = last_off
    for t in range(horizon - 1, 0, -1):
        _, prev_off, prev_sum, _ = dp[t][last_off][last_sum]
        assert prev_off is not None and prev_sum is not None
        result[t - 1] = prev_off
        last_off = prev_off
        last_sum = prev_sum

    # Calculate actual thermal energy buffer evolution from optimal path
    # This uses the buffer_kwh values stored in the DP table
    buffer_energy_evolution: list[float] = []
    last_off = best_off
    last_sum = best_sum

    # Reconstruct buffer evolution from DP table
    temp_buffer_list = []
    for t in range(horizon - 1, -1, -1):
        _, _, _, buffer_kwh = dp[t][last_off][last_sum]
        temp_buffer_list.append(buffer_kwh)
        if t > 0:
            _, prev_off, prev_sum, _ = dp[t][last_off][last_sum]
            assert prev_off is not None and prev_sum is not None
            last_off = prev_off
            last_sum = prev_sum

    # Reverse to get chronological order
    buffer_energy_evolution = list(reversed(temp_buffer_list))

    _LOGGER.debug(
        "Optimized offsets result=%s buffer_evolution=%s",
        result,
        buffer_energy_evolution,
    )

    return result, buffer_energy_evolution


def calculate_buffer_energy(
    offsets: list[int],
    demand: list[float],
    *,
    time_base: int,
    buffer: float = 0.0,
) -> list[float]:
    """Convert offset evolution to stored thermal energy in kWh.

    Physical model:
    - When offset > 0: supply temperature is raised, building is overheated,
      excess thermal energy is stored in building's thermal mass
    - When offset < 0: supply temperature is lowered, building uses stored
      thermal energy from its thermal mass
    - Energy stored per time step = offset × demand × storage_efficiency × time

    Units verification:
    - offset: °C (temperature adjustment)
    - demand: kW (thermal power demand)
    - storage_efficiency: dimensionless (fraction of demand stored per °C)
    - time: hours
    - Result: °C × kW × (dimensionless) × hours = kWh ✓

    Args:
        offsets: Temperature offsets in °C for each time step
        demand: Heat demand in kW for each time step
        time_base: Time base in minutes per step
        buffer: Initial buffer energy in kWh (default 0.0)

    Returns:
        Cumulative thermal energy buffer in kWh at each time step
    """
    if time_base <= 0:
        step_hours = 1.0
    else:
        step_hours = time_base / 60.0

    energy_evolution: list[float] = []
    buffer_energy = buffer

    for idx, offset in enumerate(offsets):
        if idx < len(demand):
            current_demand = float(demand[idx])
        else:
            current_demand = 0.0

        # Calculate energy stored/released in this time step
        # Two mechanisms:
        # 1. Active heating (demand >= 0): offset determines buffer change
        #    - Positive offset stores energy, negative offset uses stored energy
        # 2. Solar charging (demand < 0): passive buffer increase from solar gain
        if current_demand >= 0:
            energy_delta = (
                offset
                * current_demand
                * DEFAULT_THERMAL_STORAGE_EFFICIENCY
                * step_hours
            )
        else:
            # Solar gain - buffer increases by excess solar energy
            energy_delta = abs(current_demand) * step_hours

        buffer_energy += energy_delta
        energy_evolution.append(round(buffer_energy, 3))

    return energy_evolution

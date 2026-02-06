"""Battery physics model for the Battery Controller integration."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any


@dataclass
class BatteryConfig:
    """Battery configuration parameters."""

    capacity_kwh: float = 10.0
    usable_capacity_kwh: float | None = None
    max_charge_power_kw: float = 5.0
    max_discharge_power_kw: float = 5.0
    round_trip_efficiency: float = 0.90
    min_soc_percent: float = 10.0
    max_soc_percent: float = 90.0

    # DC-coupled PV configuration
    # When PV panels are connected directly to the battery inverter (DC side),
    # the charge path is: PV -> MPPT -> Battery (DC-DC, ~97% efficient)
    # vs AC-coupled: PV -> inverter -> AC -> charger -> Battery (~85% efficient)
    pv_dc_coupled: bool = False
    pv_dc_peak_power_kwp: float = 0.0
    pv_dc_efficiency: float = 0.97  # MPPT + DC-DC conversion efficiency

    # Derived values (calculated in __post_init__)
    charge_efficiency: float = field(init=False)
    discharge_efficiency: float = field(init=False)
    min_soc_kwh: float = field(init=False)
    max_soc_kwh: float = field(init=False)

    def __post_init__(self) -> None:
        """Calculate derived values."""
        # Split RTE equally between charge and discharge for AC path
        self.charge_efficiency = math.sqrt(self.round_trip_efficiency)
        self.discharge_efficiency = math.sqrt(self.round_trip_efficiency)

        # Calculate usable capacity if not specified
        if self.usable_capacity_kwh is None:
            self.usable_capacity_kwh = (
                self.capacity_kwh
                * (self.max_soc_percent - self.min_soc_percent)
                / 100.0
            )

        # Calculate SoC limits in kWh
        self.min_soc_kwh = self.capacity_kwh * self.min_soc_percent / 100.0
        self.max_soc_kwh = self.capacity_kwh * self.max_soc_percent / 100.0

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> BatteryConfig:
        """Create BatteryConfig from Home Assistant config dict."""
        from .const import (
            CONF_CAPACITY_KWH,
            CONF_USABLE_CAPACITY_KWH,
            CONF_MAX_CHARGE_POWER_KW,
            CONF_MAX_DISCHARGE_POWER_KW,
            CONF_ROUND_TRIP_EFFICIENCY,
            CONF_MIN_SOC_PERCENT,
            CONF_MAX_SOC_PERCENT,
            CONF_PV_DC_COUPLED,
            CONF_PV_DC_PEAK_POWER_KWP,
            CONF_PV_DC_EFFICIENCY,
            DEFAULT_CAPACITY_KWH,
            DEFAULT_MAX_CHARGE_POWER_KW,
            DEFAULT_MAX_DISCHARGE_POWER_KW,
            DEFAULT_ROUND_TRIP_EFFICIENCY,
            DEFAULT_MIN_SOC_PERCENT,
            DEFAULT_MAX_SOC_PERCENT,
            DEFAULT_PV_DC_COUPLED,
            DEFAULT_PV_DC_PEAK_POWER_KWP,
            DEFAULT_PV_DC_EFFICIENCY,
        )

        return cls(
            capacity_kwh=float(config.get(CONF_CAPACITY_KWH, DEFAULT_CAPACITY_KWH)),
            usable_capacity_kwh=config.get(CONF_USABLE_CAPACITY_KWH),
            max_charge_power_kw=float(
                config.get(CONF_MAX_CHARGE_POWER_KW, DEFAULT_MAX_CHARGE_POWER_KW)
            ),
            max_discharge_power_kw=float(
                config.get(CONF_MAX_DISCHARGE_POWER_KW, DEFAULT_MAX_DISCHARGE_POWER_KW)
            ),
            round_trip_efficiency=float(
                config.get(CONF_ROUND_TRIP_EFFICIENCY, DEFAULT_ROUND_TRIP_EFFICIENCY)
            ),
            min_soc_percent=float(
                config.get(CONF_MIN_SOC_PERCENT, DEFAULT_MIN_SOC_PERCENT)
            ),
            max_soc_percent=float(
                config.get(CONF_MAX_SOC_PERCENT, DEFAULT_MAX_SOC_PERCENT)
            ),
            pv_dc_coupled=bool(config.get(CONF_PV_DC_COUPLED, DEFAULT_PV_DC_COUPLED)),
            pv_dc_peak_power_kwp=float(
                config.get(CONF_PV_DC_PEAK_POWER_KWP, DEFAULT_PV_DC_PEAK_POWER_KWP)
            ),
            pv_dc_efficiency=float(
                config.get(CONF_PV_DC_EFFICIENCY, DEFAULT_PV_DC_EFFICIENCY)
            ),
        )


@dataclass
class BatteryState:
    """Current battery state."""

    soc_kwh: float = 0.0
    soc_percent: float = 0.0
    power_kw: float = 0.0
    mode: str = "idle"  # 'idle', 'charging', 'discharging'
    cycles_today: float = 0.0

    @classmethod
    def from_soc_kwh(cls, soc_kwh: float, capacity_kwh: float) -> BatteryState:
        """Create BatteryState from SoC in kWh."""
        soc_percent = (soc_kwh / capacity_kwh) * 100.0 if capacity_kwh > 0 else 0.0
        return cls(soc_kwh=soc_kwh, soc_percent=soc_percent)

    @classmethod
    def from_soc_percent(cls, soc_percent: float, capacity_kwh: float) -> BatteryState:
        """Create BatteryState from SoC in percent."""
        soc_kwh = (soc_percent / 100.0) * capacity_kwh
        return cls(soc_kwh=soc_kwh, soc_percent=soc_percent)


def calculate_efficiency(
    power_kw: float,
    soc_percent: float,
    config: BatteryConfig,
) -> float:
    """Calculate efficiency based on power level and SoC.

    Efficiency varies with:
    - Power (lower C-rate = higher efficiency)
    - SoC (efficiency drops at very low/high SoC)

    Args:
        power_kw: Current power in kW (positive = charging)
        soc_percent: Current state of charge in percent
        config: Battery configuration

    Returns:
        Efficiency multiplier (0.0-1.0)
    """
    # Base efficiency from RTE
    if power_kw >= 0:
        base_eff = config.charge_efficiency
    else:
        base_eff = config.discharge_efficiency

    # C-rate penalty (higher power = lower efficiency)
    # 2% penalty per 0.5C above 0.5C
    c_rate = abs(power_kw) / config.capacity_kwh
    c_rate_factor = 1.0 - 0.02 * max(0, c_rate - 0.5) / 0.5

    # SoC penalty (efficiency drops at extremes)
    soc_factor = 1.0
    if soc_percent < 20 or soc_percent > 80:
        soc_factor = 0.98
    if soc_percent < 10 or soc_percent > 90:
        soc_factor = 0.95

    return base_eff * c_rate_factor * soc_factor


def calculate_new_soc(
    current_soc_kwh: float,
    power_kw: float,
    duration_hours: float,
    config: BatteryConfig,
) -> tuple[float, float]:
    """Calculate new SoC after applying power for duration.

    Args:
        current_soc_kwh: Current state of charge in kWh
        power_kw: Power in kW (positive = charging, negative = discharging)
        duration_hours: Duration in hours
        config: Battery configuration

    Returns:
        Tuple of (new_soc_kwh, actual_energy_kwh)
        actual_energy_kwh is the energy actually stored/released (after efficiency)
    """
    current_soc_percent = (current_soc_kwh / config.capacity_kwh) * 100.0

    if power_kw > 0:
        # Charging
        efficiency = calculate_efficiency(power_kw, current_soc_percent, config)
        energy_stored = power_kw * duration_hours * efficiency
        new_soc = min(current_soc_kwh + energy_stored, config.max_soc_kwh)
        actual_energy = new_soc - current_soc_kwh
    elif power_kw < 0:
        # Discharging
        efficiency = calculate_efficiency(power_kw, current_soc_percent, config)
        energy_released = abs(power_kw) * duration_hours
        energy_from_battery = energy_released / efficiency
        new_soc = max(current_soc_kwh - energy_from_battery, config.min_soc_kwh)
        actual_energy = current_soc_kwh - new_soc
    else:
        # Idle
        new_soc = current_soc_kwh
        actual_energy = 0.0

    return new_soc, actual_energy


def calculate_max_charge_power(
    current_soc_kwh: float,
    duration_hours: float,
    config: BatteryConfig,
) -> float:
    """Calculate maximum charge power considering SoC limits.

    Args:
        current_soc_kwh: Current state of charge in kWh
        duration_hours: Duration in hours
        config: Battery configuration

    Returns:
        Maximum charge power in kW
    """
    # Energy needed to reach max SoC
    energy_headroom = config.max_soc_kwh - current_soc_kwh

    if energy_headroom <= 0 or duration_hours <= 0:
        return 0.0

    # Power needed to fill in duration (before efficiency)
    current_soc_percent = (current_soc_kwh / config.capacity_kwh) * 100.0
    efficiency = calculate_efficiency(
        config.max_charge_power_kw, current_soc_percent, config
    )

    power_for_headroom = energy_headroom / (duration_hours * efficiency)

    return min(power_for_headroom, config.max_charge_power_kw)


def calculate_max_discharge_power(
    current_soc_kwh: float,
    duration_hours: float,
    config: BatteryConfig,
) -> float:
    """Calculate maximum discharge power considering SoC limits.

    Args:
        current_soc_kwh: Current state of charge in kWh
        duration_hours: Duration in hours
        config: Battery configuration

    Returns:
        Maximum discharge power in kW (as positive value)
    """
    # Energy available above min SoC
    energy_available = current_soc_kwh - config.min_soc_kwh

    if energy_available <= 0 or duration_hours <= 0:
        return 0.0

    # Power needed to drain in duration (after efficiency)
    current_soc_percent = (current_soc_kwh / config.capacity_kwh) * 100.0
    efficiency = calculate_efficiency(
        -config.max_discharge_power_kw, current_soc_percent, config
    )

    power_for_available = energy_available * efficiency / duration_hours

    return min(power_for_available, config.max_discharge_power_kw)


def should_cycle(
    buy_price: float,
    sell_price: float,
    rte: float,
    degradation_per_kwh: float,
) -> bool:
    """Check if cycling the battery is profitable.

    Only cycle if profitable after RTE losses and degradation costs.

    Minimum spread needed:
    sell_price > buy_price / rte + degradation_per_kwh * 2

    Example with RTE=0.90, degradation=0.03:
    sell_price > buy_price / 0.90 + 0.06
    At buy_price = 0.10: sell_price > 0.17 needed

    Args:
        buy_price: Price to buy electricity (EUR/kWh)
        sell_price: Price to sell electricity (EUR/kWh)
        rte: Round trip efficiency (0-1)
        degradation_per_kwh: Degradation cost per kWh throughput

    Returns:
        True if cycling is profitable
    """
    min_sell_price = buy_price / rte + degradation_per_kwh * 2
    return sell_price > min_sell_price


def calculate_degradation_cost_per_kwh(
    replacement_cost_per_kwh: float = 500.0,
    lifecycle_cycles: int = 6000,
    dod_factor: float = 0.8,
) -> float:
    """Calculate degradation cost per kWh throughput.

    Args:
        replacement_cost_per_kwh: Battery replacement cost per kWh capacity
        lifecycle_cycles: Number of cycles at given DoD
        dod_factor: Depth of discharge factor (0-1)

    Returns:
        Degradation cost per kWh throughput (EUR/kWh)
    """
    # Cost per cycle = replacement_cost / lifecycle_cycles
    cost_per_cycle = replacement_cost_per_kwh / lifecycle_cycles

    # Energy per cycle = 2 * capacity * DoD (charge + discharge)
    # Cost per kWh = cost_per_cycle / (2 * DoD)
    cost_per_kwh = cost_per_cycle / (2 * dod_factor)

    return cost_per_kwh

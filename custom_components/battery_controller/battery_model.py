"""Battery physics model for the Battery Controller integration."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from .const import (
    C_RATE_PENALTY_PER_THRESHOLD,
    C_RATE_THRESHOLD,
    SOC_DERATING_EXTREME_FACTOR,
    SOC_DERATING_EXTREME_HIGH,
    SOC_DERATING_EXTREME_LOW,
    SOC_DERATING_MODERATE_FACTOR,
    SOC_DERATING_MODERATE_HIGH,
    SOC_DERATING_MODERATE_LOW,
)


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

    # Grid capacity cap: maximum import/export power at grid connection (0 = unlimited)
    max_grid_power_kw: float = 0.0

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
    def from_subentry(cls, data: dict[str, Any]) -> BatteryConfig:
        """Create BatteryConfig from a battery subentry data dict."""
        from .const import (
            CONF_CAPACITY_KWH,
            CONF_MAX_CHARGE_POWER_KW,
            CONF_MAX_DISCHARGE_POWER_KW,
            CONF_ROUND_TRIP_EFFICIENCY,
            CONF_MIN_SOC_PERCENT,
            CONF_MAX_SOC_PERCENT,
            CONF_PV_DC_EFFICIENCY,
            DEFAULT_CAPACITY_KWH,
            DEFAULT_MAX_CHARGE_POWER_KW,
            DEFAULT_MAX_DISCHARGE_POWER_KW,
            DEFAULT_ROUND_TRIP_EFFICIENCY,
            DEFAULT_MIN_SOC_PERCENT,
            DEFAULT_MAX_SOC_PERCENT,
            DEFAULT_PV_DC_EFFICIENCY,
        )

        return cls(
            capacity_kwh=float(data.get(CONF_CAPACITY_KWH, DEFAULT_CAPACITY_KWH)),
            max_charge_power_kw=float(
                data.get(CONF_MAX_CHARGE_POWER_KW, DEFAULT_MAX_CHARGE_POWER_KW)
            ),
            max_discharge_power_kw=float(
                data.get(CONF_MAX_DISCHARGE_POWER_KW, DEFAULT_MAX_DISCHARGE_POWER_KW)
            ),
            round_trip_efficiency=float(
                data.get(CONF_ROUND_TRIP_EFFICIENCY, DEFAULT_ROUND_TRIP_EFFICIENCY)
            ),
            min_soc_percent=float(
                data.get(CONF_MIN_SOC_PERCENT, DEFAULT_MIN_SOC_PERCENT)
            ),
            max_soc_percent=float(
                data.get(CONF_MAX_SOC_PERCENT, DEFAULT_MAX_SOC_PERCENT)
            ),
            pv_dc_efficiency=float(
                data.get(CONF_PV_DC_EFFICIENCY, DEFAULT_PV_DC_EFFICIENCY)
            ),
        )

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
            CONF_MAX_GRID_POWER_KW,
            DEFAULT_CAPACITY_KWH,
            DEFAULT_MAX_CHARGE_POWER_KW,
            DEFAULT_MAX_DISCHARGE_POWER_KW,
            DEFAULT_ROUND_TRIP_EFFICIENCY,
            DEFAULT_MIN_SOC_PERCENT,
            DEFAULT_MAX_SOC_PERCENT,
            DEFAULT_PV_DC_COUPLED,
            DEFAULT_PV_DC_PEAK_POWER_KWP,
            DEFAULT_PV_DC_EFFICIENCY,
            DEFAULT_MAX_GRID_POWER_KW,
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
            max_grid_power_kw=float(
                config.get(CONF_MAX_GRID_POWER_KW, DEFAULT_MAX_GRID_POWER_KW)
            ),
        )


def aggregate_battery_configs(configs: list[BatteryConfig]) -> BatteryConfig:
    """Aggregate multiple BatteryConfigs into one combined config for the optimizer.

    Capacity and power limits are summed.  RTE and SoC limits are
    capacity-weighted averages so the aggregate SoC constraints (in kWh)
    equal the sum of the individual ones.
    """
    if not configs:
        return BatteryConfig()
    if len(configs) == 1:
        return configs[0]

    total_cap = sum(c.capacity_kwh for c in configs)
    # Capacity-weighted RTE
    weighted_rte = (
        sum(c.round_trip_efficiency * c.capacity_kwh for c in configs) / total_cap
    )
    # SoC limits: sum of kWh limits, expressed back as % of combined capacity
    total_min_kwh = sum(c.min_soc_kwh for c in configs)
    total_max_kwh = sum(c.max_soc_kwh for c in configs)
    combined_min_pct = total_min_kwh / total_cap * 100.0
    combined_max_pct = total_max_kwh / total_cap * 100.0
    # DC PV: aggregate across all batteries
    dc_configs = [c for c in configs if c.pv_dc_coupled]
    pv_dc_coupled = bool(dc_configs)
    pv_dc_peak = sum(c.pv_dc_peak_power_kwp for c in configs)
    pv_dc_eff = (
        sum(c.pv_dc_efficiency * c.pv_dc_peak_power_kwp for c in dc_configs)
        / sum(c.pv_dc_peak_power_kwp for c in dc_configs)
        if dc_configs and sum(c.pv_dc_peak_power_kwp for c in dc_configs) > 0
        else 0.97
    )

    # Feed-in cap: sum of individual caps (0 = unlimited for any → unlimited overall)
    feed_in_caps = [c.max_grid_power_kw for c in configs]
    combined_feed_in_kw = (
        0.0 if any(cap == 0.0 for cap in feed_in_caps) else sum(feed_in_caps)
    )

    return BatteryConfig(
        capacity_kwh=total_cap,
        max_charge_power_kw=sum(c.max_charge_power_kw for c in configs),
        max_discharge_power_kw=sum(c.max_discharge_power_kw for c in configs),
        round_trip_efficiency=weighted_rte,
        min_soc_percent=combined_min_pct,
        max_soc_percent=combined_max_pct,
        pv_dc_coupled=pv_dc_coupled,
        pv_dc_peak_power_kwp=pv_dc_peak,
        pv_dc_efficiency=pv_dc_eff,
        max_grid_power_kw=combined_feed_in_kw,
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
    c_rate = abs(power_kw) / config.capacity_kwh
    c_rate_factor = (
        1.0
        - C_RATE_PENALTY_PER_THRESHOLD
        * max(0, c_rate - C_RATE_THRESHOLD)
        / C_RATE_THRESHOLD
    )

    # SoC penalty (efficiency drops at extremes)
    soc_factor = 1.0
    if (
        soc_percent < SOC_DERATING_MODERATE_LOW
        or soc_percent > SOC_DERATING_MODERATE_HIGH
    ):
        soc_factor = SOC_DERATING_MODERATE_FACTOR
    if (
        soc_percent < SOC_DERATING_EXTREME_LOW
        or soc_percent > SOC_DERATING_EXTREME_HIGH
    ):
        soc_factor = SOC_DERATING_EXTREME_FACTOR

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

"""Battery physics model for the Battery Controller integration."""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from typing import Any

from .const import (
    DEFAULT_HIGH_SOC_CHARGE_THRESHOLD_PCT,
    DEFAULT_LOW_SOC_DISCHARGE_THRESHOLD_PCT,
)
from .efficiency_curve import (
    EfficiencyCurve,
    aggregate_curves,
    parse_efficiency_curve,
    representative_efficiency,
)


@dataclass
class BatteryConfig:
    """Battery configuration parameters."""

    capacity_kwh: float = 10.0
    usable_capacity_kwh: float | None = None
    max_charge_power_kw: float = 5.0
    max_discharge_power_kw: float = 5.0

    # Efficiency curves: plain float string ("0.92") or power:eff pairs ("0:0.90, 5:0.95")
    # Default 0.9487 per direction = sqrt(0.90) → round-trip efficiency 0.90,
    # matching the pre-curve scalar default.
    charge_efficiency_curve: str = "0.9487"
    discharge_efficiency_curve: str = "0.9487"

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

    # SoC-dependent power derating
    # Some batteries cap charge/discharge power near SoC extremes (BMS absorption).
    # high_soc_max_charge_kw = 0 means no derating (use max_charge_power_kw always).
    # low_soc_max_discharge_kw = 0 means no derating (use max_discharge_power_kw always).
    high_soc_charge_threshold_pct: float = 100.0
    high_soc_max_charge_kw: float = 0.0
    low_soc_discharge_threshold_pct: float = 0.0
    low_soc_max_discharge_kw: float = 0.0

    # Derived values (calculated in __post_init__)
    charge_efficiency_curve_parsed: EfficiencyCurve = field(init=False)
    discharge_efficiency_curve_parsed: EfficiencyCurve = field(init=False)
    charge_efficiency: float = field(init=False)
    discharge_efficiency: float = field(init=False)
    round_trip_efficiency: float = field(init=False)
    min_soc_kwh: float = field(init=False)
    max_soc_kwh: float = field(init=False)

    def __post_init__(self) -> None:
        """Calculate derived values."""
        self.charge_efficiency_curve_parsed = parse_efficiency_curve(
            self.charge_efficiency_curve, self.max_charge_power_kw
        )
        self.discharge_efficiency_curve_parsed = parse_efficiency_curve(
            self.discharge_efficiency_curve, self.max_discharge_power_kw
        )
        # Representative scalar efficiency (mean over 5..95 % of nominal power) —
        # used by the oscillation filter, the hybrid-mode thresholds and
        # diagnostics.  See representative_efficiency() for why this is not
        # sampled at zero power.
        self.charge_efficiency = representative_efficiency(
            self.charge_efficiency_curve_parsed, self.max_charge_power_kw
        )
        self.discharge_efficiency = representative_efficiency(
            self.discharge_efficiency_curve_parsed, self.max_discharge_power_kw
        )
        # Derived scalar RTE for diagnostics / backward-compat consumers
        self.round_trip_efficiency = self.charge_efficiency * self.discharge_efficiency

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

    def max_charge_at_soc(self, soc_kwh: float) -> float:
        """Return the max charge power (kW) allowed at the given SoC.

        Above high_soc_charge_threshold_pct the BMS limits charge power to
        high_soc_max_charge_kw.  A value of 0 for the derated limit means
        no derating is configured; the nominal max is returned instead.
        """
        if (
            self.high_soc_max_charge_kw > 0
            and soc_kwh / self.capacity_kwh * 100 >= self.high_soc_charge_threshold_pct
        ):
            return self.high_soc_max_charge_kw
        return self.max_charge_power_kw

    def max_discharge_at_soc(self, soc_kwh: float) -> float:
        """Return the max discharge power (kW) allowed at the given SoC.

        Below low_soc_discharge_threshold_pct the BMS limits discharge power to
        low_soc_max_discharge_kw.  A value of 0 for the derated limit means
        no derating is configured; the nominal max is returned instead.
        """
        if (
            self.low_soc_max_discharge_kw > 0
            and soc_kwh / self.capacity_kwh * 100
            <= self.low_soc_discharge_threshold_pct
        ):
            return self.low_soc_max_discharge_kw
        return self.max_discharge_power_kw

    @classmethod
    def from_subentry(cls, data: dict[str, Any]) -> BatteryConfig:
        """Create BatteryConfig from a battery subentry data dict."""
        from .const import (
            CONF_CAPACITY_KWH,
            CONF_CHARGE_EFFICIENCY_CURVE,
            CONF_DISCHARGE_EFFICIENCY_CURVE,
            CONF_HIGH_SOC_CHARGE_THRESHOLD_PCT,
            CONF_HIGH_SOC_MAX_CHARGE_KW,
            CONF_LOW_SOC_DISCHARGE_THRESHOLD_PCT,
            CONF_LOW_SOC_MAX_DISCHARGE_KW,
            CONF_MAX_CHARGE_POWER_KW,
            CONF_MAX_DISCHARGE_POWER_KW,
            CONF_MAX_SOC_PERCENT,
            CONF_MIN_SOC_PERCENT,
            CONF_PV_DC_EFFICIENCY,
            CONF_ROUND_TRIP_EFFICIENCY,
            DEFAULT_CAPACITY_KWH,
            DEFAULT_HIGH_SOC_CHARGE_THRESHOLD_PCT,
            DEFAULT_HIGH_SOC_MAX_CHARGE_KW,
            DEFAULT_LOW_SOC_DISCHARGE_THRESHOLD_PCT,
            DEFAULT_LOW_SOC_MAX_DISCHARGE_KW,
            DEFAULT_MAX_CHARGE_POWER_KW,
            DEFAULT_MAX_DISCHARGE_POWER_KW,
            DEFAULT_MAX_SOC_PERCENT,
            DEFAULT_MIN_SOC_PERCENT,
            DEFAULT_PV_DC_EFFICIENCY,
            DEFAULT_ROUND_TRIP_EFFICIENCY,
        )

        # Backward compat: if new curve keys absent, derive flat curves from scalar RTE
        rte = float(data.get(CONF_ROUND_TRIP_EFFICIENCY, DEFAULT_ROUND_TRIP_EFFICIENCY))
        sqrt_rte_str = f"{math.sqrt(rte):.6f}"
        charge_curve_str = data.get(CONF_CHARGE_EFFICIENCY_CURVE, sqrt_rte_str)
        discharge_curve_str = data.get(CONF_DISCHARGE_EFFICIENCY_CURVE, sqrt_rte_str)

        return cls(
            capacity_kwh=float(data.get(CONF_CAPACITY_KWH, DEFAULT_CAPACITY_KWH)),
            max_charge_power_kw=float(
                data.get(CONF_MAX_CHARGE_POWER_KW, DEFAULT_MAX_CHARGE_POWER_KW)
            ),
            max_discharge_power_kw=float(
                data.get(CONF_MAX_DISCHARGE_POWER_KW, DEFAULT_MAX_DISCHARGE_POWER_KW)
            ),
            charge_efficiency_curve=charge_curve_str,
            discharge_efficiency_curve=discharge_curve_str,
            min_soc_percent=float(
                data.get(CONF_MIN_SOC_PERCENT, DEFAULT_MIN_SOC_PERCENT)
            ),
            max_soc_percent=float(
                data.get(CONF_MAX_SOC_PERCENT, DEFAULT_MAX_SOC_PERCENT)
            ),
            pv_dc_efficiency=float(
                data.get(CONF_PV_DC_EFFICIENCY, DEFAULT_PV_DC_EFFICIENCY)
            ),
            high_soc_charge_threshold_pct=float(
                data.get(
                    CONF_HIGH_SOC_CHARGE_THRESHOLD_PCT,
                    DEFAULT_HIGH_SOC_CHARGE_THRESHOLD_PCT,
                )
            ),
            high_soc_max_charge_kw=float(
                data.get(CONF_HIGH_SOC_MAX_CHARGE_KW, DEFAULT_HIGH_SOC_MAX_CHARGE_KW)
            ),
            low_soc_discharge_threshold_pct=float(
                data.get(
                    CONF_LOW_SOC_DISCHARGE_THRESHOLD_PCT,
                    DEFAULT_LOW_SOC_DISCHARGE_THRESHOLD_PCT,
                )
            ),
            low_soc_max_discharge_kw=float(
                data.get(
                    CONF_LOW_SOC_MAX_DISCHARGE_KW, DEFAULT_LOW_SOC_MAX_DISCHARGE_KW
                )
            ),
        )

    @classmethod
    def _from_aggregated(
        cls,
        *,
        capacity_kwh: float,
        max_charge_power_kw: float,
        max_discharge_power_kw: float,
        charge_curve: EfficiencyCurve,
        discharge_curve: EfficiencyCurve,
        min_soc_percent: float,
        max_soc_percent: float,
        pv_dc_coupled: bool,
        pv_dc_peak_power_kwp: float,
        pv_dc_efficiency: float,
        max_grid_power_kw: float,
        high_soc_charge_threshold_pct: float,
        high_soc_max_charge_kw: float,
        low_soc_discharge_threshold_pct: float,
        low_soc_max_discharge_kw: float,
    ) -> BatteryConfig:
        """Create BatteryConfig from pre-parsed aggregated curves."""

        # Serialize the curves to strings so the normal constructor path works.
        # Fixed-point formatting: repr() would emit scientific notation for very
        # small values, which parse_efficiency_curve does not accept.
        def _curve_to_str(curve: EfficiencyCurve) -> str:
            return ", ".join(f"{p:.6f}:{e:.6f}" for p, e in curve)

        return cls(
            capacity_kwh=capacity_kwh,
            max_charge_power_kw=max_charge_power_kw,
            max_discharge_power_kw=max_discharge_power_kw,
            charge_efficiency_curve=_curve_to_str(charge_curve),
            discharge_efficiency_curve=_curve_to_str(discharge_curve),
            min_soc_percent=min_soc_percent,
            max_soc_percent=max_soc_percent,
            pv_dc_coupled=pv_dc_coupled,
            pv_dc_peak_power_kwp=pv_dc_peak_power_kwp,
            pv_dc_efficiency=pv_dc_efficiency,
            max_grid_power_kw=max_grid_power_kw,
            high_soc_charge_threshold_pct=high_soc_charge_threshold_pct,
            high_soc_max_charge_kw=high_soc_max_charge_kw,
            low_soc_discharge_threshold_pct=low_soc_discharge_threshold_pct,
            low_soc_max_discharge_kw=low_soc_max_discharge_kw,
        )


def aggregate_battery_configs(configs: list[BatteryConfig]) -> BatteryConfig:
    """Aggregate multiple BatteryConfigs into one combined config for the optimizer.

    Capacity and power limits are summed.  Efficiency curves are
    capacity-weighted averages.  SoC limits are capacity-weighted so the
    aggregate SoC constraints (in kWh) equal the sum of the individual ones.
    """
    if not configs:
        return BatteryConfig()
    if len(configs) == 1:
        # Deep-copied, not returned as-is: the caller overlays entry-level
        # settings (DC coupling, grid cap) onto the aggregate, and sharing the
        # object would mutate the single battery's own config through it.
        return copy.deepcopy(configs[0])

    total_cap = sum(c.capacity_kwh for c in configs)

    # Efficiency curves are indexed by power, so they are combined on the power
    # axis: each battery carries a share of the aggregate power proportional to
    # its own rating and is evaluated at that share, not at the fleet total.
    combined_charge_curve = aggregate_curves(
        [c.charge_efficiency_curve_parsed for c in configs],
        [c.max_charge_power_kw for c in configs],
        direction="charge",
    )
    combined_discharge_curve = aggregate_curves(
        [c.discharge_efficiency_curve_parsed for c in configs],
        [c.max_discharge_power_kw for c in configs],
        direction="discharge",
    )

    # SoC limits: sum of kWh limits, expressed back as % of combined capacity
    total_min_kwh = sum(c.min_soc_kwh for c in configs)
    total_max_kwh = sum(c.max_soc_kwh for c in configs)
    combined_min_pct = total_min_kwh / total_cap * 100.0
    combined_max_pct = total_max_kwh / total_cap * 100.0

    # DC PV: aggregate across all batteries.
    #
    # pv_dc_coupled is an entry-level flag (it lives on the PV-array subentries)
    # that the caller overlays afterwards, so it is never set on the inputs
    # here.  The MPPT efficiency, by contrast, IS configured per battery
    # subentry.  Weighting it by pv_dc_peak_power_kwp — also unset on battery
    # subentries — therefore always found a zero total and silently fell back
    # to the hard-coded default, discarding the configured values whenever more
    # than one battery was present (a single battery kept them, because that
    # path returns the config unchanged).  Weight by usable capacity instead:
    # it is always populated, and it is what determines how much DC PV each
    # inverter can actually absorb.
    pv_dc_coupled = any(c.pv_dc_coupled for c in configs)
    pv_dc_peak = sum(c.pv_dc_peak_power_kwp for c in configs)
    eff_weights = [max(0.0, c.max_soc_kwh - c.min_soc_kwh) for c in configs]
    total_eff_weight = sum(eff_weights)
    pv_dc_eff = (
        sum(c.pv_dc_efficiency * w for c, w in zip(configs, eff_weights))
        / total_eff_weight
        if total_eff_weight > 0
        else sum(c.pv_dc_efficiency for c in configs) / len(configs)
    )

    # Grid cap: sum of individual caps (0 = unlimited for any → unlimited
    # overall). In the integration this is always overwritten afterwards by
    # OptimizationCoordinator._apply_entry_level_config, because the cap is a
    # property of the house connection rather than of any battery; the
    # aggregation below only matters to direct callers of this function.
    feed_in_caps = [c.max_grid_power_kw for c in configs]
    combined_feed_in_kw = (
        0.0 if any(cap == 0.0 for cap in feed_in_caps) else sum(feed_in_caps)
    )

    # SoC-dependent derating.  The fleet is assumed to sit at a common relative
    # SoC, so above the fleet threshold the combined limit is the sum of what
    # each battery can still do there: its own derated limit if it derates,
    # its full rating if it does not.
    #
    # The threshold is the FIRST one reached (lowest for charge, highest for
    # discharge), not a capacity-weighted average.  Averaging mixed the
    # thresholds of batteries that derate with the disabling sentinels of
    # batteries that do not (100 % / 0 %), and pairing that average with a sum
    # of only the derated powers capped the whole fleet at one battery's
    # reduced rating — a 5 kW pack without derating lost 5 kW of charge power
    # because its neighbour throttles above 90 %.  Applying the first
    # threshold to the summed per-battery limits is conservative in timing
    # (derating may start slightly early for some packs) but never understates
    # the power the fleet can deliver.
    charge_derated = [c for c in configs if c.high_soc_max_charge_kw > 0]
    if charge_derated:
        combined_high_threshold = min(
            c.high_soc_charge_threshold_pct for c in charge_derated
        )
        combined_high_max_charge_kw = sum(
            c.high_soc_max_charge_kw
            if c.high_soc_max_charge_kw > 0
            else c.max_charge_power_kw
            for c in configs
        )
    else:
        # No battery derates: keep the disabled sentinel (kw == 0) so
        # max_charge_at_soc returns the nominal rating at every SoC, and the
        # sentinel threshold to match. A capacity-weighted average of the
        # per-battery thresholds used to be computed here, which read as if it
        # mattered — max_charge_at_soc never looks at it while the kW limit is 0.
        combined_high_threshold = DEFAULT_HIGH_SOC_CHARGE_THRESHOLD_PCT
        combined_high_max_charge_kw = 0.0

    discharge_derated = [c for c in configs if c.low_soc_max_discharge_kw > 0]
    if discharge_derated:
        combined_low_threshold = max(
            c.low_soc_discharge_threshold_pct for c in discharge_derated
        )
        combined_low_max_discharge_kw = sum(
            c.low_soc_max_discharge_kw
            if c.low_soc_max_discharge_kw > 0
            else c.max_discharge_power_kw
            for c in configs
        )
    else:
        combined_low_threshold = DEFAULT_LOW_SOC_DISCHARGE_THRESHOLD_PCT
        combined_low_max_discharge_kw = 0.0

    return BatteryConfig._from_aggregated(
        capacity_kwh=total_cap,
        max_charge_power_kw=sum(c.max_charge_power_kw for c in configs),
        max_discharge_power_kw=sum(c.max_discharge_power_kw for c in configs),
        charge_curve=combined_charge_curve,
        discharge_curve=combined_discharge_curve,
        min_soc_percent=combined_min_pct,
        max_soc_percent=combined_max_pct,
        pv_dc_coupled=pv_dc_coupled,
        pv_dc_peak_power_kwp=pv_dc_peak,
        pv_dc_efficiency=pv_dc_eff,
        max_grid_power_kw=combined_feed_in_kw,
        high_soc_charge_threshold_pct=combined_high_threshold,
        high_soc_max_charge_kw=combined_high_max_charge_kw,
        low_soc_discharge_threshold_pct=combined_low_threshold,
        low_soc_max_discharge_kw=combined_low_max_discharge_kw,
    )


@dataclass
class BatteryState:
    """Current battery state."""

    soc_kwh: float = 0.0
    soc_percent: float = 0.0
    power_kw: float = 0.0
    mode: str = "idle"  # 'idle', 'charging', 'discharging'
    cycles_today: float = 0.0

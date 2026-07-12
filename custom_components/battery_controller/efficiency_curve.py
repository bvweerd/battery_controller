"""Power-dependent efficiency curve model for battery charge/discharge."""

from __future__ import annotations

import re

EfficiencyCurve = list[tuple[float, float]]
"""List of (power_kw, efficiency) tuples, sorted ascending by power."""

_PAIR_RE = re.compile(r"\(?\s*(-?\d+(?:\.\d+)?)\s*[,:\s]\s*(-?\d+(?:\.\d+)?)\s*\)?")


def parse_efficiency_curve(value: str | float, max_power_kw: float) -> EfficiencyCurve:
    """Parse an efficiency curve from user input.

    Accepted formats:
    - Plain float/int: ``0.95``  →  flat curve at that efficiency
    - String float:    ``"0.95"``  →  same
    - Power:eff pairs: ``"0:0.92, 5:0.95"``  →  [(0.0, 0.92), (5.0, 0.95)]
    - Tuple style:     ``"(0, 0.92), (5, 0.95)"``

    Returns a list of (power_kw, efficiency) tuples sorted by power_kw.

    Raises ValueError on malformed input.
    """
    raw = str(value).strip()

    # Try to parse as a single number first
    try:
        scalar = float(raw)
        if not (0.0 < scalar <= 1.0):
            raise ValueError(f"Efficiency value {scalar!r} must be in range (0, 1]")
        return [(0.0, scalar), (float(max_power_kw), scalar)]
    except ValueError as exc:
        if "must be in range" in str(exc):
            raise
        # Not a plain float; try to parse as pairs

    pairs = _PAIR_RE.findall(raw)
    if not pairs:
        raise ValueError(
            f"Cannot parse efficiency curve from {value!r}. "
            "Use a plain float like '0.95' or power:efficiency pairs like '0:0.92, 5:0.95'."
        )

    # Reject inputs where numbers are left over after pairing (e.g. a bare
    # list of efficiencies like "0.92 0.95 0.97"): silently dropping the
    # remainder would accept nonsense power values without the user noticing.
    leftover = _PAIR_RE.sub("", raw)
    if re.search(r"\d", leftover):
        raise ValueError(
            f"Cannot parse efficiency curve from {value!r}: "
            f"unpaired number(s) {leftover.strip(' ,;')!r} left over. "
            "Use power:efficiency pairs like '0:0.92, 5:0.95'."
        )

    curve: dict[float, float] = {}
    for power_str, eff_str in pairs:
        power = float(power_str)
        eff = float(eff_str)
        if power < 0:
            raise ValueError(f"Power value {power} must be >= 0")
        if not (0.0 < eff <= 1.0):
            raise ValueError(f"Efficiency value {eff} must be in range (0, 1]")
        curve[power] = eff  # last one wins on duplicate power

    if not curve:
        raise ValueError(f"No valid efficiency points found in {value!r}")

    return sorted(curve.items())


def interpolate_efficiency(curve: EfficiencyCurve, power_kw: float) -> float:
    """Return interpolated efficiency for a given power level.

    Linear interpolation between curve points. Flat clamping outside range.
    An empty curve yields 1.0 (no losses) — this matches the mirrored
    implementations in docs/analyzer.js and simulate/simulate_diagnostics.py.
    """
    if not curve:
        return 1.0
    if len(curve) == 1:
        return curve[0][1]

    if power_kw <= curve[0][0]:
        return curve[0][1]

    for (p0, e0), (p1, e1) in zip(curve, curve[1:]):
        if power_kw <= p1:
            if p1 == p0:
                return e1
            t = (power_kw - p0) / (p1 - p0)
            return e0 + t * (e1 - e0)

    return curve[-1][1]


def aggregate_curves(
    curves: list[EfficiencyCurve], weights: list[float]
) -> EfficiencyCurve:
    """Combine multiple efficiency curves using capacity-weighted averaging.

    Resamples all curves at the union of their power breakpoints (plus 0 and
    the maximum power across all curves), then computes a weighted-average
    efficiency at each point.

    Args:
        curves: Per-battery efficiency curves.
        weights: Capacity weights (e.g. capacity_kwh), one per curve.

    Returns:
        A single aggregated EfficiencyCurve sorted by power.
    """
    if not curves:
        raise ValueError("At least one curve is required")
    if len(curves) != len(weights):
        raise ValueError("curves and weights must have the same length")

    total_weight = sum(weights)
    if total_weight <= 0:
        raise ValueError("Total weight must be positive")

    # Collect all power breakpoints
    power_points: set[float] = {0.0}
    for curve in curves:
        for p, _ in curve:
            power_points.add(p)
        power_points.add(curve[-1][0])  # max of this curve

    combined: EfficiencyCurve = []
    for p in sorted(power_points):
        avg_eff = (
            sum(
                interpolate_efficiency(curve, p) * w
                for curve, w in zip(curves, weights)
            )
            / total_weight
        )
        combined.append((p, avg_eff))

    return combined

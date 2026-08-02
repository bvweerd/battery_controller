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


# Load points used to reduce a curve to a single representative scalar.
# 10 points from 5 % to 95 % of nominal power in 10 % steps — the same sampling
# the HTW Berlin "Stromspeicher-Inspektion" efficiency guideline uses for its
# mean path efficiencies, so the scalar is comparable to published figures.
_REPRESENTATIVE_LOAD_POINTS = tuple(0.05 + 0.10 * i for i in range(10))


def representative_efficiency(curve: EfficiencyCurve, max_power_kw: float) -> float:
    """Reduce a curve to one scalar describing its typical operating efficiency.

    Used wherever a single efficiency number is needed instead of the full
    curve: the oscillation-filter arbitrage threshold, the hybrid-mode shadow
    price comparisons and the derived ``round_trip_efficiency``.

    The value is the arithmetic mean of the curve sampled at 5 %..95 % of
    nominal power.  Sampling at zero power instead would pick the single worst
    point of a realistic curve: a home battery system's efficiency is dominated
    by the inverter's fixed idle loss at low power, so curves rise steeply from
    near-zero power and then flatten.  A zero-power scalar therefore understates
    round-trip efficiency badly (0.64 instead of 0.93 for a measured curve),
    which inflates every arbitrage threshold derived from it.

    A flat curve yields exactly its own value, so scalar-configured batteries
    behave identically to the pre-curve implementation.
    """
    if not curve:
        return 1.0
    if max_power_kw <= 0:
        return interpolate_efficiency(curve, 0.0)
    return sum(
        interpolate_efficiency(curve, f * max_power_kw)
        for f in _REPRESENTATIVE_LOAD_POINTS
    ) / len(_REPRESENTATIVE_LOAD_POINTS)


def aggregate_curves(
    curves: list[EfficiencyCurve],
    max_powers: list[float],
    *,
    direction: str = "charge",
) -> EfficiencyCurve:
    """Combine per-battery efficiency curves into one curve for the fleet.

    The curves are indexed by *power*, so they cannot simply be averaged at the
    same absolute power: when the aggregate runs at P kW, each battery only
    carries its own share of that power.  Power is assumed to be split in
    proportion to each battery's power rating, so battery *i* operates at
    ``share_i * P`` and is evaluated there.

    The two directions combine differently because efficiency enters the SoC
    transition differently (``stored = P * eff`` when charging, ``drawn =
    P / eff`` when discharging):

    - ``charge``:    ``eff(P) = sum(share_i * eff_i(share_i * P))``
    - ``discharge``: ``eff(P) = 1 / sum(share_i / eff_i(share_i * P))``

    Args:
        curves: Per-battery efficiency curves.
        max_powers: Per-battery power rating in kW, one per curve. Determines
            both the power split and the range of the aggregated curve.
        direction: ``"charge"`` or ``"discharge"``.

    Returns:
        A single aggregated EfficiencyCurve sorted by power, spanning
        0..sum(max_powers).
    """
    if not curves:
        raise ValueError("At least one curve is required")
    if len(curves) != len(max_powers):
        raise ValueError("curves and max_powers must have the same length")
    if direction not in ("charge", "discharge"):
        raise ValueError(
            f"direction must be 'charge' or 'discharge', got {direction!r}"
        )

    total_power = sum(max_powers)
    if total_power <= 0:
        # No pack can move any power, so there is no power axis to split along.
        # Fall back to an equal-weight average so the caller still gets a valid
        # curve (the optimizer will only ever evaluate it at zero power).
        shares = [1.0 / len(curves)] * len(curves)
        total_power = 0.0
    else:
        shares = [p / total_power for p in max_powers]

    # Breakpoints: a breakpoint at power p of battery i is reached when the
    # aggregate runs at p / share_i, so map each curve's points onto that axis.
    power_points: set[float] = {0.0, total_power}
    for curve, share in zip(curves, shares):
        if share <= 0:
            continue
        for p, _ in curve:
            aggregate_p = p / share
            if 0.0 < aggregate_p < total_power:
                power_points.add(aggregate_p)

    combined: EfficiencyCurve = []
    for p in sorted(power_points):
        effs = [
            interpolate_efficiency(curve, share * p)
            for curve, share in zip(curves, shares)
        ]
        if direction == "charge":
            eff = sum(share * e for share, e in zip(shares, effs))
        else:
            eff = 1.0 / sum(share / e for share, e in zip(shares, effs) if e > 0)
        combined.append((p, min(1.0, eff)))

    return combined

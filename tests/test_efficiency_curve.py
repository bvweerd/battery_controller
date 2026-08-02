"""Tests for efficiency_curve module."""

import pytest

from custom_components.battery_controller.efficiency_curve import (
    aggregate_curves,
    interpolate_efficiency,
    parse_efficiency_curve,
    representative_efficiency,
)


class TestParseEfficiencyCurve:
    def test_plain_float(self):
        result = parse_efficiency_curve(0.95, max_power_kw=5.0)
        assert result == [(0.0, 0.95), (5.0, 0.95)]

    def test_string_float(self):
        result = parse_efficiency_curve("0.90", max_power_kw=10.0)
        assert result == [(0.0, 0.90), (10.0, 0.90)]

    def test_integer_string(self):
        # "1" is a valid scalar efficiency of 1.0
        result = parse_efficiency_curve("1", max_power_kw=5.0)
        assert result == [(0.0, 1.0), (5.0, 1.0)]

    def test_colon_separated_pairs(self):
        result = parse_efficiency_curve("0:0.90, 5:0.95", max_power_kw=10.0)
        assert result == [(0.0, 0.90), (5.0, 0.95)]

    def test_tuple_style(self):
        result = parse_efficiency_curve("(0, 0.90), (5, 0.95)", max_power_kw=10.0)
        assert result == [(0.0, 0.90), (5.0, 0.95)]

    def test_semicolon_delimited(self):
        result = parse_efficiency_curve("0:0.90; 5:0.95", max_power_kw=10.0)
        assert result == [(0.0, 0.90), (5.0, 0.95)]

    def test_sorted_by_power(self):
        result = parse_efficiency_curve("5:0.95, 0:0.90", max_power_kw=10.0)
        assert result == [(0.0, 0.90), (5.0, 0.95)]

    def test_duplicate_power_last_wins(self):
        result = parse_efficiency_curve("0:0.88, 0:0.90, 5:0.95", max_power_kw=10.0)
        assert result[0] == (0.0, 0.90)

    def test_three_points(self):
        result = parse_efficiency_curve("0:0.88, 3:0.92, 6:0.95", max_power_kw=10.0)
        assert len(result) == 3
        assert result == [(0.0, 0.88), (3.0, 0.92), (6.0, 0.95)]

    def test_error_efficiency_above_one(self):
        with pytest.raises(ValueError, match="range"):
            parse_efficiency_curve(1.1, max_power_kw=5.0)

    def test_error_efficiency_below_zero(self):
        with pytest.raises(ValueError, match="range"):
            parse_efficiency_curve(-0.1, max_power_kw=5.0)

    def test_error_efficiency_zero(self):
        with pytest.raises(ValueError, match="range"):
            parse_efficiency_curve(0.0, max_power_kw=5.0)

    def test_error_negative_power_in_pairs(self):
        with pytest.raises(ValueError, match="Power value"):
            parse_efficiency_curve("-1:0.90, 5:0.95", max_power_kw=10.0)

    def test_error_out_of_range_efficiency_in_pairs(self):
        with pytest.raises(ValueError, match="Efficiency value"):
            parse_efficiency_curve("0:1.1, 5:0.95", max_power_kw=10.0)

    def test_error_bare_number_list(self):
        # A bare list of efficiencies pairs up the first two numbers and
        # would silently drop the rest — must be rejected instead.
        with pytest.raises(ValueError, match="unpaired"):
            parse_efficiency_curve("0.92 0.95 0.97", max_power_kw=5.0)

    def test_error_malformed_string(self):
        with pytest.raises(ValueError, match="Cannot parse"):
            parse_efficiency_curve("not_a_number", max_power_kw=5.0)

    def test_max_power_used_for_flat_curve(self):
        result = parse_efficiency_curve(0.92, max_power_kw=7.5)
        assert result == [(0.0, 0.92), (7.5, 0.92)]


class TestInterpolateEfficiency:
    def test_exact_hit_first_point(self):
        curve = [(0.0, 0.90), (5.0, 0.95)]
        assert interpolate_efficiency(curve, 0.0) == pytest.approx(0.90)

    def test_exact_hit_last_point(self):
        curve = [(0.0, 0.90), (5.0, 0.95)]
        assert interpolate_efficiency(curve, 5.0) == pytest.approx(0.95)

    def test_midpoint_interpolation(self):
        curve = [(0.0, 0.80), (10.0, 1.0)]
        assert interpolate_efficiency(curve, 5.0) == pytest.approx(0.90)

    def test_between_two_interior_points(self):
        curve = [(0.0, 0.88), (3.0, 0.92), (6.0, 0.95)]
        # Between 3.0 and 6.0: t=0.5 → 0.92 + 0.5*(0.95-0.92) = 0.935
        assert interpolate_efficiency(curve, 4.5) == pytest.approx(0.935)

    def test_below_min_returns_first(self):
        curve = [(2.0, 0.90), (5.0, 0.95)]
        assert interpolate_efficiency(curve, 0.0) == pytest.approx(0.90)

    def test_above_max_returns_last(self):
        curve = [(0.0, 0.90), (5.0, 0.95)]
        assert interpolate_efficiency(curve, 10.0) == pytest.approx(0.95)

    def test_empty_curve_returns_one(self):
        # Must match docs/analyzer/analyzer.js and simulate/simulate_diagnostics.py,
        # which both return 1.0 (no losses) for an empty curve.
        assert interpolate_efficiency([], 2.0) == 1.0

    def test_single_point_curve(self):
        curve = [(0.0, 0.92)]
        assert interpolate_efficiency(curve, 5.0) == pytest.approx(0.92)
        assert interpolate_efficiency(curve, 0.0) == pytest.approx(0.92)

    def test_exact_second_point(self):
        curve = [(0.0, 0.88), (3.0, 0.92), (6.0, 0.95)]
        assert interpolate_efficiency(curve, 3.0) == pytest.approx(0.92)


class TestAggregateCurves:
    """aggregate_curves combines on the POWER axis.

    Each battery carries a share of the aggregate power proportional to its own
    rating, so battery i is evaluated at share_i * P — not at the fleet total.
    """

    def test_two_flat_curves_equal_power(self):
        curve_a = [(0.0, 0.90), (5.0, 0.90)]
        curve_b = [(0.0, 0.80), (5.0, 0.80)]
        result = aggregate_curves([curve_a, curve_b], [5.0, 5.0])
        # Flat curves: charge direction is the share-weighted arithmetic mean
        for _, eff in result:
            assert eff == pytest.approx(0.85)

    def test_two_flat_curves_unequal_power(self):
        curve_a = [(0.0, 0.90), (3.0, 0.90)]
        curve_b = [(0.0, 0.70), (1.0, 0.70)]
        result = aggregate_curves([curve_a, curve_b], [3.0, 1.0])
        # (0.90*3 + 0.70*1) / 4 = 0.85
        for _, eff in result:
            assert eff == pytest.approx(0.85)

    def test_discharge_uses_harmonic_combination(self):
        curve_a = [(0.0, 0.90), (5.0, 0.90)]
        curve_b = [(0.0, 0.70), (5.0, 0.70)]
        result = aggregate_curves([curve_a, curve_b], [5.0, 5.0], direction="discharge")
        # Discharging draws P/eff, so the shares combine harmonically:
        # 1 / (0.5/0.90 + 0.5/0.70) = 0.7875
        for _, eff in result:
            assert eff == pytest.approx(1.0 / (0.5 / 0.90 + 0.5 / 0.70))

    def test_spans_summed_power(self):
        curve = [(0.0, 0.90), (5.0, 0.95)]
        result = aggregate_curves([curve, curve], [5.0, 5.0])
        assert result[0][0] == 0.0
        assert result[-1][0] == pytest.approx(10.0)

    def test_breakpoints_mapped_onto_aggregate_axis(self):
        # Two identical 5 kW batteries: each hits its own 2 kW breakpoint when
        # the aggregate is at 4 kW, so the breakpoint appears at 4, not 2.
        curve = [(0.0, 0.60), (2.0, 0.90), (5.0, 0.95)]
        result = aggregate_curves([curve, curve], [5.0, 5.0])
        powers = [p for p, _ in result]
        assert 4.0 in powers
        assert 10.0 in powers

    def test_part_load_not_overestimated(self):
        """The bug this aggregation exists to avoid.

        Two identical 5 kW batteries at 2 kW aggregate power run at 1 kW each,
        so the fleet efficiency must be the curve at 1 kW, not at 2 kW.
        """
        curve = [(0.0, 0.50), (1.0, 0.70), (2.0, 0.90), (5.0, 0.95)]
        result = aggregate_curves([curve, curve], [5.0, 5.0])
        eff_at_2kw = interpolate_efficiency(result, 2.0)
        assert eff_at_2kw == pytest.approx(0.70)
        assert eff_at_2kw != pytest.approx(0.90)

    def test_identical_batteries_reproduce_own_curve(self):
        """N identical batteries behave exactly like one scaled-up battery."""
        curve = [(0.0, 0.50), (1.0, 0.70), (2.0, 0.90), (5.0, 0.95)]
        result = aggregate_curves([curve, curve, curve], [5.0, 5.0, 5.0])
        for aggregate_kw in (0.0, 1.5, 3.0, 6.0, 9.0, 15.0):
            assert interpolate_efficiency(result, aggregate_kw) == pytest.approx(
                interpolate_efficiency(curve, aggregate_kw / 3.0)
            )

    def test_single_curve_unchanged(self):
        curve = [(0.0, 0.90), (5.0, 0.95)]
        result = aggregate_curves([curve], [5.0])
        assert result == [(0.0, 0.90), (5.0, 0.95)]

    def test_result_sorted_by_power(self):
        curve_a = [(0.0, 0.90), (3.0, 0.92)]
        curve_b = [(0.0, 0.88), (5.0, 0.93)]
        result = aggregate_curves([curve_a, curve_b], [3.0, 5.0])
        powers = [p for p, _ in result]
        assert powers == sorted(powers)

    def test_error_bad_direction(self):
        curve = [(0.0, 0.90), (5.0, 0.90)]
        with pytest.raises(ValueError, match="direction must be"):
            aggregate_curves([curve], [5.0], direction="sideways")

    def test_zero_total_power_falls_back_to_equal_weights(self):
        """A fleet that cannot move power still needs a usable curve."""
        curve_a = [(0.0, 0.90), (5.0, 0.90)]
        curve_b = [(0.0, 0.70), (5.0, 0.70)]
        result = aggregate_curves([curve_a, curve_b], [0.0, 0.0])
        assert result
        assert interpolate_efficiency(result, 0.0) == pytest.approx(0.80)

    def test_error_empty_curves(self):
        with pytest.raises(ValueError, match="At least one"):
            aggregate_curves([], [])

    def test_error_mismatched_lengths(self):
        curve = [(0.0, 0.90)]
        with pytest.raises(ValueError, match="same length"):
            aggregate_curves([curve], [1.0, 2.0])


class TestRepresentativeEfficiency:
    """The scalar that stands in for a curve wherever one number is needed."""

    def test_flat_curve_returns_its_own_value(self):
        """Scalar-configured batteries must behave exactly as before curves."""
        curve = [(0.0, 0.9487), (5.0, 0.9487)]
        assert representative_efficiency(curve, 5.0) == pytest.approx(0.9487)

    def test_mean_of_ten_load_points(self):
        # Linear 0.80 -> 1.00 over 0..10 kW. Sampling at 5%..95% in 10% steps
        # averages to the value at 50% of nominal power = 0.90.
        curve = [(0.0, 0.80), (10.0, 1.00)]
        assert representative_efficiency(curve, 10.0) == pytest.approx(0.90)

    def test_does_not_collapse_to_zero_power_value(self):
        """Regression: a realistic rising curve must not be judged by its worst point.

        Measured part-load curves start near 50-80 % because the inverter's idle
        loss dominates at low power.  Taking the zero-power value as *the*
        efficiency understates round-trip efficiency badly and inflates every
        arbitrage threshold derived from it.
        """
        # HTW 2026, RCT POWER Power Storage DC 10.0 (best measured part load)
        curve = [
            (0.05, 0.784),
            (0.1, 0.843),
            (0.2, 0.901),
            (0.3, 0.921),
            (0.5, 0.941),
            (10.0, 0.958),
        ]
        zero_power = interpolate_efficiency(curve, 0.0)
        representative = representative_efficiency(curve, 10.0)
        assert zero_power == pytest.approx(0.784)
        assert representative > 0.94
        # Round-trip: the zero-power reading is wildly pessimistic
        assert zero_power**2 < 0.62
        assert representative**2 > 0.89

    def test_empty_curve(self):
        assert representative_efficiency([], 5.0) == 1.0

    def test_zero_max_power_falls_back_to_zero_power_value(self):
        curve = [(0.0, 0.80), (10.0, 1.00)]
        assert representative_efficiency(curve, 0.0) == pytest.approx(0.80)

"""Tests for what the calibration measures and for the curve it hands the DP.

The correction is an efficiency factor: measured efficiency over the efficiency
the *user* entered. That definition is what lets one apply rule and one clamp
serve both directions. Stored as a raw measured-over-planned ratio it meant the
opposite on each side, and the module's own rule — only ever plan a battery as
slower than its curve, never as faster — then discarded the discharge derating
it exists to catch while applying the optimism it exists to ignore.
"""

from __future__ import annotations

from collections import deque

import pytest

from custom_components.battery_controller.efficiency_calibration import (
    CALIBRATION_WINDOW,
    CHARGE_CALIBRATION,
    DISCHARGE_CALIBRATION,
    curve_override,
)

# A pack whose entered curve peaks at 0.909, as a measured AC-to-AC curve does.
CURVE = [(0.2, 0.857), (0.5, 0.909), (1.2, 0.892)]


class TestEfficiencyFactor:
    """Below 1.0 must mean "slower than modelled" on both sides."""

    def test_charging_slower_than_modelled_lands_below_one(self):
        # planned = AC * eff, so a slow pack gains less SoC than planned
        assert CHARGE_CALIBRATION.efficiency_factor(0.9) == pytest.approx(0.9)

    def test_discharging_slower_than_modelled_lands_below_one(self):
        # planned = AC / eff, so a slow pack loses MORE SoC than planned
        assert DISCHARGE_CALIBRATION.efficiency_factor(1.25) == pytest.approx(0.8)

    def test_discharging_better_than_modelled_lands_above_one(self):
        assert DISCHARGE_CALIBRATION.efficiency_factor(0.8583) == pytest.approx(
            1.1651, abs=1e-4
        )

    def test_nothing_measured_is_not_a_factor(self):
        assert DISCHARGE_CALIBRATION.efficiency_factor(0.0) is None


class TestCurveOverride:
    def test_no_curve_when_the_factor_is_within_noise(self):
        assert curve_override(CURVE, 0.999) is None

    def test_no_curve_when_the_pack_beats_its_own_curve(self):
        """A measurement may confirm the user's entry, not make the DP bolder."""
        assert curve_override(CURVE, 1.05) is None

    def test_factor_scales_every_point(self):
        curve = curve_override(CURVE, 0.9)

        assert curve is not None
        assert dict(curve)[0.5] == pytest.approx(0.909 * 0.9)
        assert dict(curve)[0.2] == pytest.approx(0.857 * 0.9)

    def test_curve_never_exceeds_unity(self):
        """These are efficiencies; a battery cannot return more than it was given.

        With a factor the cap is a backstop rather than the main defence — only
        an entered curve above 1.0 can reach it, since the factor itself is
        below 1 by the time a curve is produced at all. It stays because the
        unbounded version of this line is what let an over-unity discharge point
        through, and the DP plans its actions against these numbers.
        """
        curve = curve_override([(0.5, 1.2)], 0.99)

        assert curve is not None
        assert max(eff for _p, eff in curve) == pytest.approx(1.0)


class TestDispatchFidelity:
    """The counters answer "did it do what it was told", not "what did it lose"."""

    def test_unmeasured_until_a_counter_reading_arrives(self, optimization_coordinator):
        calibration = optimization_coordinator.battery_calibrations["bat1"].charge

        assert calibration.dispatch_fidelity is None
        assert calibration.dispatch_sample_count == 0

    def test_records_measured_over_commanded(self, optimization_coordinator):
        calibration = optimization_coordinator.battery_calibrations["bat1"].charge

        calibration.record_dispatch(1.0, 0.98)
        calibration.record_dispatch(2.0, 1.96)

        assert calibration.dispatch_fidelity == pytest.approx(0.98)
        assert calibration.dispatch_sample_count == 2

    def test_zero_command_is_not_a_sample(self, optimization_coordinator):
        calibration = optimization_coordinator.battery_calibrations["bat1"].charge

        calibration.record_dispatch(0.0, 0.5)

        assert calibration.dispatch_sample_count == 0

    def test_window_is_bounded(self, optimization_coordinator):
        calibration = optimization_coordinator.battery_calibrations["bat1"].charge

        for _ in range(CALIBRATION_WINDOW + 5):
            calibration.record_dispatch(1.0, 1.0)

        assert calibration.dispatch_sample_count == CALIBRATION_WINDOW

    def test_it_never_reaches_the_correction_or_the_store(
        self, optimization_coordinator
    ):
        """An observation about the device, not a stored property of it."""
        calibration = optimization_coordinator.battery_calibrations["bat1"].charge
        calibration.samples = deque([0.97], maxlen=CALIBRATION_WINDOW)
        calibration.correction = 0.97

        calibration.record_dispatch(1.0, 0.5)

        assert calibration.correction == pytest.approx(0.97)
        assert list(calibration.samples) == [0.97]

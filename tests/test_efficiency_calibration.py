"""Tests for the curves the calibration hands the DP, and for what it refuses.

The correction itself is a ratio against the curve the *user* entered, so it may
legitimately land above 1.0 — a pessimistic entry has to be correctable upwards.
The curve built from it is a different matter: the efficiencies in it are
physical quantities with a ceiling, and the DP selects its actions against them.

The two directions used to disagree about that, and the discharge side is where
it showed: an over-unity discharge point makes the modelled round trip nearly
lossless, which turns price wiggles far below the real break-even spread into
apparent profit.
"""

from __future__ import annotations

from collections import deque

import pytest

from custom_components.battery_controller.efficiency_calibration import (
    CALIBRATION_WINDOW,
    charge_curve_override,
    discharge_curve_override,
)

# A pack whose entered curve peaks at 0.909, as a measured AC-to-AC curve does.
CURVE = [(0.2, 0.857), (0.5, 0.909), (1.2, 0.892)]


class TestDischargeCurveOverride:
    def test_no_curve_when_the_correction_is_within_noise(self):
        assert discharge_curve_override(CURVE, 0.999) is None

    def test_correction_raises_the_curve_towards_measured_behaviour(self):
        """Below the ceiling the correction passes through unchanged."""
        curve = discharge_curve_override(CURVE, 0.95)

        assert curve is not None
        assert dict(curve)[0.2] == pytest.approx(0.857 / 0.95)

    def test_curve_never_exceeds_unity(self):
        """discharge_eff is AC delivered over pack energy drawn: 1.0 is the ceiling.

        0.8583 is the correction a healthy pack acquires when an AC-side energy
        counter is scored against a SoC-denominated plan. Unbounded, it lifted
        this curve to 1.04 and the modelled round trip from 0.80 to 0.93.
        """
        curve = discharge_curve_override(CURVE, 0.8583)

        assert curve is not None
        assert max(eff for _p, eff in curve) <= 1.0
        assert dict(curve)[0.5] == pytest.approx(1.0)
        # Points that stay under the ceiling are still corrected.
        assert dict(curve)[0.2] == pytest.approx(0.857 / 0.8583)

    def test_modelled_round_trip_stays_physical(self):
        """The pair of curves may not describe a battery that gains energy."""
        discharge = discharge_curve_override(CURVE, 0.8583)
        assert discharge is not None

        for (_p, charge_eff), (_q, discharge_eff) in zip(CURVE, discharge):
            assert charge_eff * discharge_eff <= 1.0


class TestChargeCurveOverride:
    def test_no_curve_when_the_correction_is_within_noise(self):
        assert charge_curve_override(CURVE, 0.999) is None

    def test_curve_is_scaled_down_and_bounded(self):
        curve = charge_curve_override(CURVE, 0.9)

        assert curve is not None
        assert dict(curve)[0.5] == pytest.approx(0.909 * 0.9)
        assert max(eff for _p, eff in curve) <= 1.0


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

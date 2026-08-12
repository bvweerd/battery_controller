"""Learned charge/discharge efficiency corrections for the Battery Controller.

The DP plans a SoC change for every step. Reality does not always deliver it —
a battery in its CV phase charges slower than a flat efficiency curve predicts,
and an ageing pack discharges slower than its rating. This module scores the
previous plan against what the battery actually moved and folds the ratio into a
correction factor.

The correction is applied to the DP's SoC **transition** only, never to the cost
model: a charging-speed problem is not extra energy cost, and double-counting it
as one would make the optimizer avoid perfectly cheap hours.

The coordinator owns the decision of *when* a sample is eligible (that needs the
previous plan, the executed setpoint and the control mode); everything here is
the mechanics of turning an eligible observation into a factor.
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from homeassistant.helpers import storage

from .battery_model import BatteryConfig
from .const import ACTION_CHARGING, ACTION_DISCHARGING
from .efficiency_curve import EfficiencyCurve

_LOGGER = logging.getLogger(__name__)

# A sample is only worth taking when the planned SoC change is comfortably
# larger than the resolution of whatever measured it. The energy counters are
# fine-grained, so the floor there is just "big enough to be a real action". A
# SoC sensor reporting whole percent, by contrast, quantises at capacity/100 —
# 0.1 kWh on a 10 kWh pack, which is exactly the old fixed floor, so a single
# sample carried up to +/-50 % quantisation error. On that path the floor
# scales with the observed quantum instead.
CALIBRATION_MIN_DELTA_KWH = 0.1
CALIBRATION_SOC_QUANTUM_FACTOR = 4.0

# Samples outside this window are dropped rather than clipped into it. The
# window is wide enough to contain genuine derating and far enough above 1.0
# that ordinary measurement noise is not truncated on one side — the previous
# code capped the ratio at 1.05 while allowing it down to 0.5, so symmetric
# noise biased the mean downward and could apply a correction below 1.0 to a
# perfectly healthy battery. The applied correction is still clamped (below).
CALIBRATION_ACCEPT_MIN = 0.5
CALIBRATION_ACCEPT_MAX = 1.5
# Bounds on the correction actually handed to the optimizer.
CALIBRATION_APPLY_MIN = 0.5
CALIBRATION_APPLY_MAX = 1.05
# Number of observations averaged into one correction.
CALIBRATION_WINDOW = 20
# A correction is only persisted (and logged) once it moves by more than this.
CALIBRATION_SIGNIFICANT_CHANGE = 0.005
# Corrections at or above this are within measurement noise of nominal: they are
# stored but never handed to the optimizer, which gets the unmodified curve.
CALIBRATION_APPLY_THRESHOLD = 0.995

# What the last calibration attempt did, published so a user can tell a
# correction that has genuinely been measured from one that has simply never
# had the chance to learn anything.
CALIBRATION_SAMPLED = "sampled"
CALIBRATION_NO_RESULT = "no_previous_run"
CALIBRATION_NO_PLAN = "direction_not_planned"
CALIBRATION_PLAN_NOT_EXECUTED = "plan_not_executed"
CALIBRATION_STEP_INCOMPLETE = "step_not_finished"
CALIBRATION_DC_COUPLED = "dc_coupled_pv"
CALIBRATION_DELTA_TOO_SMALL = "step_too_small_to_measure"
CALIBRATION_DERATING = "step_crosses_derating_threshold"
CALIBRATION_IMPLAUSIBLE = "sample_dropped_implausible"


@dataclass(frozen=True)
class CalibrationSpec:
    """What distinguishes the charge- from the discharge-side calibration.

    Both sides answer the same question — did the battery move as much energy
    as the DP planned for the step — so they share one implementation, and only
    the direction-dependent details live here.
    """

    name: str  # "charge" / "discharge", used in log messages
    action: str  # planned first-step mode that makes a sample eligible
    counter_key: str  # key into the cumulative throughput counters
    derate_label: str  # "high-SoC" / "low-SoC", used in log messages
    # Inverter derating within the step invalidates a sample; these read the
    # relevant limit and its SoC threshold off the aggregated battery config.
    derate_limit_kw: Callable[[BatteryConfig], float]
    derate_threshold_pct: Callable[[BatteryConfig], float]


CHARGE_CALIBRATION = CalibrationSpec(
    name="charge",
    action=ACTION_CHARGING,
    counter_key="charged",
    derate_label="high-SoC",
    derate_limit_kw=lambda bc: bc.high_soc_max_charge_kw,
    derate_threshold_pct=lambda bc: bc.high_soc_charge_threshold_pct,
)
DISCHARGE_CALIBRATION = CalibrationSpec(
    name="discharge",
    action=ACTION_DISCHARGING,
    counter_key="discharged",
    derate_label="low-SoC",
    derate_limit_kw=lambda bc: bc.low_soc_max_discharge_kw,
    derate_threshold_pct=lambda bc: bc.low_soc_discharge_threshold_pct,
)


def min_planned_delta_kwh(from_counters: bool, soc_quantum_kwh: float) -> float:
    """Smallest planned SoC change worth sampling, given how it is measured."""
    if from_counters:
        return CALIBRATION_MIN_DELTA_KWH
    return max(
        CALIBRATION_MIN_DELTA_KWH,
        CALIBRATION_SOC_QUANTUM_FACTOR * soc_quantum_kwh,
    )


def counter_delta_kwh(
    key: str, before: float | None, after: float | None
) -> float | None:
    """Throughput measured by the counters since the plan was made.

    None when either end of the interval is missing, or when the counter went
    backwards — a total_increasing sensor that was reset or replaced, where the
    difference is meaningless rather than negative.
    """
    if before is None or after is None:
        return None
    delta = after - before
    if delta < 0:
        _LOGGER.debug(
            "Efficiency calibration: %s counter went backwards "
            "(%.3f -> %.3f kWh); treating as a meter reset",
            key,
            before,
            after,
        )
        return None
    return delta


def charge_curve_override(
    curve: EfficiencyCurve, correction: float
) -> EfficiencyCurve | None:
    """Curve for the DP's charge-side SoC transition, or None to keep nominal.

    When the battery charges slower than modelled, the DP should plan less
    charge within the step, so each point is scaled down by the correction.
    """
    if correction >= CALIBRATION_APPLY_THRESHOLD:
        return None
    return [(p, min(1.0, eff * correction)) for p, eff in curve]


def discharge_curve_override(
    curve: EfficiencyCurve, correction: float
) -> EfficiencyCurve | None:
    """Curve for the DP's discharge-side SoC transition, or None to keep nominal.

    The transition is ``soc -= power * hours / discharge_eff``, so reducing the
    planned SoC drop by ``correction`` needs a LARGER efficiency: each point is
    DIVIDED by the correction, not multiplied. Points may exceed 1.0, which is
    safe precisely because this curve never enters the cost model.
    """
    if correction >= CALIBRATION_APPLY_THRESHOLD:
        return None
    return [(p, max(1e-6, eff / correction)) for p, eff in curve]


@dataclass
class DirectionCalibration:
    """Rolling window of actual/planned ratios for one direction, persisted.

    Kept as its own object per direction so charge and discharge cannot share
    state by accident, and so the storage key, the sample window and the
    correction always travel together.
    """

    spec: CalibrationSpec
    store: storage.Store[dict[str, Any]]
    samples: deque[float] = field(
        default_factory=lambda: deque(maxlen=CALIBRATION_WINDOW)
    )
    correction: float = 1.0
    # Why the last attempt did or did not move the correction. Published so a
    # user can tell a correction that has genuinely been measured from one that
    # has never had the chance to learn anything.
    last_result: str = CALIBRATION_NO_RESULT

    @property
    def applied(self) -> bool:
        """Whether this correction currently changes the DP's plan.

        Same gate as charge_curve_override / discharge_curve_override: a
        correction at or above CALIBRATION_APPLY_THRESHOLD is within
        measurement noise of nominal, so it is stored but never handed to the
        optimizer.
        """
        return self.correction < CALIBRATION_APPLY_THRESHOLD

    @property
    def sample_count(self) -> int:
        """Number of observations behind the current correction."""
        return len(self.samples)

    async def async_load(self) -> None:
        """Restore the persisted samples and correction, if any."""
        stored = await self.store.async_load()
        if stored is None:
            return
        self.samples = deque(stored.get("samples", []), maxlen=CALIBRATION_WINDOW)
        self.correction = float(stored.get("correction", 1.0))
        if self.correction < CALIBRATION_APPLY_THRESHOLD:
            _LOGGER.info(
                "Restored %s efficiency calibration: correction=%.3f, n=%d samples",
                self.spec.name,
                self.correction,
                len(self.samples),
            )

    async def async_save(self) -> None:
        """Persist the current samples and correction."""
        await self.store.async_save(
            {"samples": list(self.samples), "correction": self.correction}
        )

    async def async_reset(self) -> None:
        """Reset to the nominal uncorrected state and persist that."""
        if self.samples or abs(self.correction - 1.0) > 1e-9:
            _LOGGER.info(
                "Resetting %s efficiency calibration: %.3f (%d samples) -> 1.000",
                self.spec.name,
                self.correction,
                len(self.samples),
            )
        self.samples.clear()
        self.correction = 1.0
        self.last_result = CALIBRATION_NO_RESULT
        await self.async_save()

    def record(self, planned_delta: float, actual_delta: float, source: str) -> bool:
        """Fold one actual/planned observation in; return whether it moved.

        Also sets ``last_result`` to the outcome, so a dropped sample is
        distinguishable from one that was never eligible.

        Samples outside the acceptance window are dropped rather than clipped
        into it. Clipping was not symmetric around 1.0 — the ratio was capped at
        1.05 but allowed down to 0.5 — so ordinary measurement noise pulled the
        mean below 1.0 and a healthy battery could end up with a correction
        applied to it. Only the resulting mean is clamped, and only for use.
        """
        ratio = actual_delta / planned_delta
        if not (CALIBRATION_ACCEPT_MIN <= ratio <= CALIBRATION_ACCEPT_MAX):
            _LOGGER.debug(
                "%s efficiency calibration: dropping implausible sample "
                "(ratio=%.3f from %s, planned Δ=%.2f kWh, actual Δ=%.2f kWh)",
                self.spec.name.capitalize(),
                ratio,
                source,
                planned_delta,
                actual_delta,
            )
            self.last_result = CALIBRATION_IMPLAUSIBLE
            return False

        previous = self.correction
        self.samples.append(ratio)
        mean = sum(self.samples) / len(self.samples)
        self.correction = max(CALIBRATION_APPLY_MIN, min(CALIBRATION_APPLY_MAX, mean))
        self.last_result = CALIBRATION_SAMPLED
        moved = abs(self.correction - previous) > CALIBRATION_SIGNIFICANT_CHANGE
        if moved:
            _LOGGER.info(
                "%s efficiency correction updated: %.3f → %.3f "
                "(latest ratio=%.3f from %s, n=%d samples, "
                "planned Δ=%.2f kWh, actual Δ=%.2f kWh)",
                self.spec.name.capitalize(),
                previous,
                self.correction,
                ratio,
                source,
                len(self.samples),
                planned_delta,
                actual_delta,
            )
        return moved

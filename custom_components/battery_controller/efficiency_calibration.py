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
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from homeassistant.helpers import storage

from .battery_model import BatteryConfig
from .const import ACTION_CHARGING, ACTION_DISCHARGING
from .efficiency_curve import EfficiencyCurve

_LOGGER = logging.getLogger(__name__)

# A sample is only worth taking when the planned SoC change is comfortably
# larger than the resolution of what measured it. A SoC sensor reporting whole
# percent quantises at capacity/100 — 0.1 kWh on a 10 kWh pack, which is exactly
# the old fixed floor, so a single sample carried up to +/-50 % quantisation
# error. The floor therefore scales with the observed quantum.
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
CALIBRATION_NO_SOC_SOURCE = "no_soc_measurement"
CALIBRATION_NO_PLAN = "direction_not_planned"
CALIBRATION_NOT_DISPATCHED = "battery_not_dispatched"
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


def min_planned_delta_kwh(soc_quantum_kwh: float) -> float:
    """Smallest planned SoC change worth sampling at the SoC sensor's resolution."""
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
    DIVIDED by the correction, not multiplied.

    A correction above 1.0 is legitimate — it means the pack outperforms the
    curve the user entered, and pessimistic entries must be allowed to be
    corrected upwards. The resulting *curve* is bounded at 1.0 all the same,
    because ``discharge_eff`` is defined as AC delivered over pack energy drawn:
    a point above 1.0 says the inverter puts out more than it takes, and the DP
    then plans discharges the battery cannot deliver. The charge side has always
    bounded its curve this way; this one used to be justified as harmless
    "because it never enters the cost model", which is true of the accounting
    and false of the decisions — the DP chooses its actions against exactly this
    transition, and an over-unity round trip makes flat price wiggles look
    profitable.
    """
    if correction >= CALIBRATION_APPLY_THRESHOLD:
        return None
    return [(p, min(1.0, max(1e-6, eff / correction))) for p, eff in curve]


@dataclass
class DirectionCalibration:
    """Rolling window of actual/planned ratios for one direction, persisted.

    Kept as its own object per direction so charge and discharge cannot share
    state by accident, and so the storage key, the sample window and the
    correction always travel together.
    """

    spec: CalibrationSpec
    store: storage.Store[dict[str, Any]]
    # Human-readable owner of this calibration ("Marstek"), used in log
    # messages so a multi-battery system says which pack it is talking about.
    label: str = ""
    # Where to look when this calibration has never been persisted; see
    # async_load. None for a calibration that has no predecessor.
    legacy_store: storage.Store[dict[str, Any]] | None = None
    samples: deque[float] = field(
        default_factory=lambda: deque(maxlen=CALIBRATION_WINDOW)
    )
    correction: float = 1.0
    # Why the last attempt did or did not move the correction. Published so a
    # user can tell a correction that has genuinely been measured from one that
    # has never had the chance to learn anything.
    last_result: str = CALIBRATION_NO_RESULT
    # Measured throughput over commanded throughput, from the energy counters.
    # Deliberately not persisted and never applied: it is an observation about
    # the device, not a stored property of it. See record_dispatch.
    dispatch_samples: deque[float] = field(
        default_factory=lambda: deque(maxlen=CALIBRATION_WINDOW)
    )

    @property
    def dispatch_fidelity(self) -> float | None:
        """Mean measured/commanded AC throughput, or None if never measured."""
        if not self.dispatch_samples:
            return None
        return sum(self.dispatch_samples) / len(self.dispatch_samples)

    @property
    def dispatch_sample_count(self) -> int:
        """Observations behind dispatch_fidelity."""
        return len(self.dispatch_samples)

    def record_dispatch(self, commanded_kwh: float, measured_kwh: float) -> None:
        """Fold in one measured-over-commanded observation from the counters.

        This is not an efficiency. A battery energy counter sits on the same
        side of the inverter as the setpoint that drove it — for an AC-coupled
        pack there is no other side to put it on — so the ratio says whether the
        device delivered what it was told to, not what it lost doing so. Feeding
        it to the curve as if it were an efficiency measures the conversion loss
        a second time and cancels it out, which is how a discharge correction
        near the nominal efficiency (and a charge correction pinned to its clamp)
        arises on a perfectly healthy pack.

        Published as a diagnostic instead: a device that stops following its
        setpoint is worth seeing, and this is the number that shows it.
        """
        if commanded_kwh <= 0:
            return
        self.dispatch_samples.append(measured_kwh / commanded_kwh)

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

    @property
    def who(self) -> str:
        """ "Charge" / "Charge (Marstek 2)" — the subject of a log line."""
        return f"{self.spec.name.capitalize()}" + (
            f" ({self.label})" if self.label else ""
        )

    async def async_load(self) -> None:
        """Restore the persisted samples and correction, if any.

        A battery with nothing of its own falls back to ``legacy_store``, the
        fleet-wide store this calibration used to share with every other pack.
        That value was measured on whichever battery the dispatcher happened to
        pick, so it is no longer the answer — but it is a far better prior than
        nominal, and it is replaced sample by sample as the pack measures
        itself.
        """
        stored = await self.store.async_load()
        if stored is None and self.legacy_store is not None:
            stored = await self.legacy_store.async_load()
            if stored is not None:
                _LOGGER.info(
                    "Seeding %s efficiency calibration for %s from the previous "
                    "fleet-wide correction",
                    self.spec.name,
                    self.label or "battery",
                )
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
        self.dispatch_samples.clear()
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
                self.who,
                ratio,
                source,
                planned_delta,
                actual_delta,
            )
            self.last_result = CALIBRATION_IMPLAUSIBLE
            return False

        previous = self.correction
        self.samples.append(ratio)
        # A ratio is only meaningful next to the ratios of the same battery, so
        # the mean below is per battery; the fleet number the DP uses is
        # assembled from these by aggregate_correction().
        mean = sum(self.samples) / len(self.samples)
        self.correction = max(CALIBRATION_APPLY_MIN, min(CALIBRATION_APPLY_MAX, mean))
        self.last_result = CALIBRATION_SAMPLED
        moved = abs(self.correction - previous) > CALIBRATION_SIGNIFICANT_CHANGE
        if moved:
            _LOGGER.info(
                "%s efficiency correction updated: %.3f → %.3f "
                "(latest ratio=%.3f from %s, n=%d samples, "
                "planned Δ=%.2f kWh, actual Δ=%.2f kWh)",
                self.who,
                previous,
                self.correction,
                ratio,
                source,
                len(self.samples),
                planned_delta,
                actual_delta,
            )
        return moved


@dataclass
class BatteryCalibration:
    """Both directions' calibration for one battery.

    Every pack gets its own, because the dispatcher does not spread a setpoint
    evenly: it concentrates on one battery at a time, so a single fleet-wide
    ratio describes whichever pack happened to be dispatched and then attributes
    it to all of them. A pack that is losing capacity is invisible that way — it
    only drags the shared number down and has its healthy sibling planned
    pessimistically.
    """

    subentry_id: str
    charge: DirectionCalibration
    discharge: DirectionCalibration

    def for_action(self, action: str) -> DirectionCalibration:
        """The direction whose plan is ``action``."""
        return self.charge if action == ACTION_CHARGING else self.discharge

    def both(self) -> tuple[DirectionCalibration, DirectionCalibration]:
        """Both directions, for the operations that treat them alike."""
        return (self.charge, self.discharge)


def aggregate_correction(weighted: Iterable[tuple[float, float]]) -> float:
    """Combine per-battery corrections into the one the DP plans with.

    The DP has a single SoC state for the whole fleet, so it needs a single
    factor. Weighting is by usable capacity over the batteries that have
    actually measured something: a pack the dispatcher has never used carries no
    evidence, and letting its nominal 1.0 dilute a measured sibling would report
    half the derating that was observed. An unmeasured pack is therefore assumed
    to behave like the measured ones rather than like the datasheet.

    Returns 1.0 when nothing has been measured at all.
    """
    total = 0.0
    total_weight = 0.0
    for correction, weight in weighted:
        if weight <= 0:
            continue
        total += correction * weight
        total_weight += weight
    return total / total_weight if total_weight > 0 else 1.0


def aggregate_last_result(results: Iterable[str]) -> str:
    """The fleet's headline calibration outcome.

    A sample taken on any battery is the informative answer for the fleet —
    the reasons the others skipped are visible on their own sensors.
    """
    reasons = list(results)
    if not reasons:
        return CALIBRATION_NO_RESULT
    if CALIBRATION_SAMPLED in reasons:
        return CALIBRATION_SAMPLED
    return reasons[0]

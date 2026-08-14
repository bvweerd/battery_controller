"""Learned charge/discharge efficiency corrections for the Battery Controller.

The DP plans a SoC change for every step. Reality does not always deliver it —
a battery in its CV phase charges slower than a flat efficiency curve predicts,
and an ageing pack discharges slower than its rating. This module scores the
previous plan against what the battery actually moved and folds the result into a
correction factor: measured efficiency over modelled efficiency, so that below
1.0 means "slower than the curve says" in both directions.

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
# Both bounds act on the efficiency factor, so they mean the same thing in
# both directions: a factor, not a raw measured-over-planned ratio.
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

# Marks a stored payload whose samples are efficiency factors. Anything without
# it holds raw measured/planned ratios, which mean the opposite on the discharge
# side; see DirectionCalibration._migrate_ratios.
STORED_EFFICIENCY_FACTOR = "efficiency_factor"

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
    # Whether measured/planned has to be inverted to become an efficiency
    # factor. The planned SoC change is `AC * eff` when charging and `AC / eff`
    # when discharging, so the same observation points opposite ways: a pack
    # that is slower than modelled moves LESS SoC than planned while charging
    # and MORE while discharging. Storing the raw ratio therefore gave one
    # number two meanings — and the "only apply a correction below nominal"
    # rule, which is the whole point of the module, then discarded exactly the
    # discharge derating it was written to catch, while applying the optimistic
    # direction it was written to ignore. See efficiency_factor().
    invert_ratio: bool
    # Inverter derating within the step invalidates a sample; these read the
    # relevant limit and its SoC threshold off the aggregated battery config.
    derate_limit_kw: Callable[[BatteryConfig], float]
    derate_threshold_pct: Callable[[BatteryConfig], float]

    def efficiency_factor(self, ratio: float) -> float | None:
        """Measured efficiency over modelled efficiency, from measured/planned.

        Below 1.0 means "slower than the curve says" in both directions, which
        is what makes one apply rule and one clamp correct for both. None when
        the ratio carries no efficiency at all (nothing measured).
        """
        if ratio <= 0:
            return None
        return 1.0 / ratio if self.invert_ratio else ratio


CHARGE_CALIBRATION = CalibrationSpec(
    name="charge",
    action=ACTION_CHARGING,
    counter_key="charged",
    derate_label="high-SoC",
    invert_ratio=False,
    derate_limit_kw=lambda bc: bc.high_soc_max_charge_kw,
    derate_threshold_pct=lambda bc: bc.high_soc_charge_threshold_pct,
)
DISCHARGE_CALIBRATION = CalibrationSpec(
    name="discharge",
    action=ACTION_DISCHARGING,
    counter_key="discharged",
    derate_label="low-SoC",
    invert_ratio=True,
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


def curve_override(curve: EfficiencyCurve, correction: float) -> EfficiencyCurve | None:
    """Curve for the DP's SoC transition, or None to keep the nominal one.

    The correction is an efficiency factor (see CalibrationSpec.efficiency_
    factor), so both directions scale the same way: a pack running at 96 % of
    the curve it was given transitions as if every point were 96 %, whether it
    is filling or emptying.

    Two bounds, for two different reasons. Only a factor below
    CALIBRATION_APPLY_THRESHOLD produces a curve at all — a pack that meets or
    beats its curve is planned with the curve the user entered, because the
    measurement can confirm the entry but has no business making the DP more
    optimistic than the user was. And the result is capped at 1.0 because these
    are efficiencies: a point above 1.0 describes a battery that returns more
    than it was given, and the DP, which selects its actions against exactly
    this transition, will happily plan the resulting free round trips.
    """
    if correction >= CALIBRATION_APPLY_THRESHOLD:
        return None
    return [(p, min(1.0, max(1e-6, eff * correction))) for p, eff in curve]


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

        Same gate as curve_override(): a
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
        samples = [float(value) for value in stored.get("samples", [])]
        correction = float(stored.get("correction", 1.0))
        if stored.get("measures") != STORED_EFFICIENCY_FACTOR:
            samples, correction = self._migrate_ratios(samples, correction)
        self.samples = deque(samples, maxlen=CALIBRATION_WINDOW)
        self.correction = correction
        if self.correction < CALIBRATION_APPLY_THRESHOLD:
            _LOGGER.info(
                "Restored %s efficiency calibration: correction=%.3f, n=%d samples",
                self.spec.name,
                self.correction,
                len(self.samples),
            )

    def _migrate_ratios(
        self, samples: list[float], correction: float
    ) -> tuple[list[float], float]:
        """Convert a payload persisted as measured/planned into efficiency factors.

        Only the discharge side changes value — the charge ratio always was the
        efficiency factor — but both go through the same call so the stored
        marker means one thing.

        A discharge history full of the energy-counter artefact converts to
        factors well above nominal and lands on the clamp, where it is stored
        and never applied. That is the right destination for a measurement that
        was never an efficiency, and it is also why the converted correction is
        re-clamped here: the old bounds were applied to a number that meant
        something else.
        """
        converted = [
            factor
            for factor in (self.spec.efficiency_factor(value) for value in samples)
            if factor is not None
        ]
        migrated = self.spec.efficiency_factor(correction)
        migrated = 1.0 if migrated is None else migrated
        migrated = max(CALIBRATION_APPLY_MIN, min(CALIBRATION_APPLY_MAX, migrated))
        if self.spec.invert_ratio and (samples or correction != 1.0):
            _LOGGER.info(
                "Converted stored %s calibration to efficiency factors: "
                "%.3f -> %.3f (%d samples)",
                self.spec.name,
                correction,
                migrated,
                len(converted),
            )
        return converted, migrated

    async def async_save(self) -> None:
        """Persist the current samples and correction."""
        await self.store.async_save(
            {
                "samples": list(self.samples),
                "correction": self.correction,
                "measures": STORED_EFFICIENCY_FACTOR,
            }
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
        factor = self.spec.efficiency_factor(actual_delta / planned_delta)
        if factor is None or not (
            CALIBRATION_ACCEPT_MIN <= factor <= CALIBRATION_ACCEPT_MAX
        ):
            _LOGGER.debug(
                "%s efficiency calibration: dropping implausible sample "
                "(factor=%s from %s, planned Δ=%.2f kWh, actual Δ=%.2f kWh)",
                self.who,
                f"{factor:.3f}" if factor is not None else "n/a",
                source,
                planned_delta,
                actual_delta,
            )
            self.last_result = CALIBRATION_IMPLAUSIBLE
            return False

        previous = self.correction
        self.samples.append(factor)
        # A factor is only meaningful next to the factors of the same battery,
        # so the mean below is per battery; the fleet number the DP uses is
        # assembled from these by aggregate_correction().
        mean = sum(self.samples) / len(self.samples)
        self.correction = max(CALIBRATION_APPLY_MIN, min(CALIBRATION_APPLY_MAX, mean))
        self.last_result = CALIBRATION_SAMPLED
        moved = abs(self.correction - previous) > CALIBRATION_SIGNIFICANT_CHANGE
        if moved:
            _LOGGER.info(
                "%s efficiency correction updated: %.3f → %.3f "
                "(latest factor=%.3f from %s, n=%d samples, "
                "planned Δ=%.2f kWh, actual Δ=%.2f kWh)",
                self.who,
                previous,
                self.correction,
                factor,
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

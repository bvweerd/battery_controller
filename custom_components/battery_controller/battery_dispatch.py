"""Multi-battery setpoint dispatch for the Battery Controller integration.

One combined setpoint comes out of the optimizer; several inverters have to
carry it. How it is divided is a policy of its own — concentrate on one pack
while the fleet is balanced, split proportionally once it diverges — with
enough hysteresis state that it does not belong inline in the coordinator.
"""

from __future__ import annotations

from .battery_model import BatteryConfig, BatteryState
from .const import MODE_HYBRID, MODE_HYBRID_PLUS, MODE_ZERO_GRID

# When the relative-SoC gap between batteries is below _SOC_SPLIT_THRESHOLD,
# concentrate the full setpoint on one battery (better tracking, avoids
# low-power inefficiency). Above the threshold, split proportionally to
# rebalance diverging SoC levels.
_SOC_SPLIT_THRESHOLD = 0.10
# Minimum rel-SoC advantage for a challenger battery to displace the current
# active battery within concentration mode. Must be < _SOC_SPLIT_THRESHOLD so
# there is a middle zone [_SOC_HYSTERESIS, _SOC_SPLIT_THRESHOLD) where a switch
# to a clearly better battery happens before proportional splitting kicks in.
_SOC_HYSTERESIS = 0.05


class BatteryDispatcher:
    """Divides one combined setpoint across the configured batteries.

    Owns the selection hysteresis: which battery is currently carrying the
    setpoint has to persist across calls, or a fleet sitting near the switching
    threshold would hand the setpoint back and forth every real-time tick.
    """

    def __init__(self, configs: list[tuple[str, BatteryConfig]] | None = None) -> None:
        """Initialize with the per-battery configs, keyed by subentry id."""
        self.configs: list[tuple[str, BatteryConfig]] = configs or []
        self.states: dict[str, BatteryState] = {}
        # Active battery for concentration dispatch (None = select fresh next call).
        # zero_grid keeps one battery for both directions (stable across direction
        # changes); scheduled dispatch tracks its own, since there the direction
        # determines the selection criterion.
        self.zero_grid_active: str | None = None
        self.scheduled_active: str | None = None

    def split_setpoint(self, total_kw: float, mode: str = "") -> dict[str, float]:
        """Split combined setpoint (kW, positive=charge) to per-battery setpoints.

        ``mode`` is the user-selected CONTROL mode, not the resolved
        zero-grid-controller mode. The controller only ever reports
        ``zero_grid`` / ``follow_schedule`` / ``idle`` / ``manual``, so passing
        that made the hybrid branch below unreachable and put hybrid runs on the
        directional (charge/discharge) selection criterion, which switches
        inverters whenever the schedule reverses direction.

        Uses SoC-gap triggered concentration:
        - Gap < _SOC_SPLIT_THRESHOLD: concentrate on one battery. Avoids
          splitting tiny setpoints across inverters and provides stable
          single-inverter tracking for zero-grid corrections.
        - Gap >= _SOC_SPLIT_THRESHOLD: proportional split with iterative
          power-overflow redistribution to rebalance diverging SoC levels.

        Battery selection for concentration (with _SOC_HYSTERESIS to prevent
        rapid switching):
        - zero_grid / hybrid / hybrid+: battery closest to 50% rel_soc — handles both
          charge and discharge direction changes without switching inverters.
        - scheduled charge: battery with lowest rel_soc (most headroom).
        - scheduled discharge: battery with highest rel_soc (most energy).
        """
        if not self.configs:
            return {}
        if abs(total_kw) < 1e-6:
            return {sid: 0.0 for sid, _ in self.configs}

        rel_socs = self._compute_rel_socs()
        soc_gap = max(rel_socs.values()) - min(rel_socs.values())

        if soc_gap >= _SOC_SPLIT_THRESHOLD:
            # SoC has diverged — proportional split to rebalance; reset active batteries
            self.zero_grid_active = None
            self.scheduled_active = None
            return self._proportional_split(total_kw, self.configs)

        winner = self._select_active_battery(total_kw, rel_socs, mode)
        return self._concentrate(total_kw, winner)

    def _compute_rel_socs(self) -> dict[str, float]:
        """Compute relative SoC position [0, 1] per battery within its usable range."""
        result: dict[str, float] = {}
        for sid, cfg in self.configs:
            usable = cfg.max_soc_kwh - cfg.min_soc_kwh
            if usable > 0 and sid in self.states:
                soc = self.states[sid].soc_kwh
                result[sid] = (soc - cfg.min_soc_kwh) / usable
            else:
                result[sid] = 0.5
        return result

    def _select_active_battery(
        self, total_kw: float, rel_socs: dict[str, float], mode: str
    ) -> str:
        """Select which battery to concentrate the setpoint on.

        Applies hysteresis: only displaces the current active battery when the
        best candidate's score exceeds the current battery's by _SOC_HYSTERESIS.
        """
        sids = [sid for sid, _ in self.configs]

        if mode in (MODE_ZERO_GRID, MODE_HYBRID, MODE_HYBRID_PLUS):
            # Prefer battery closest to 50% rel_soc: stays within limits longest
            # regardless of whether the next setpoint is charge or discharge.
            def score(sid: str) -> float:
                return -abs(rel_socs[sid] - 0.5)

            active_attr = "zero_grid_active"
        elif total_kw > 0:
            # Scheduled charge: prefer lowest rel_soc (most room)
            def score(sid: str) -> float:
                return -rel_socs[sid]

            active_attr = "scheduled_active"
        else:
            # Scheduled discharge: prefer highest rel_soc (most energy)
            def score(sid: str) -> float:
                return rel_socs[sid]

            active_attr = "scheduled_active"

        best = max(sids, key=score)
        current: str | None = getattr(self, active_attr)

        if current is not None and current in rel_socs:
            if score(best) - score(current) <= _SOC_HYSTERESIS:
                best = current  # stay with current battery

        setattr(self, active_attr, best)
        return best

    def _concentrate(self, total_kw: float, winner: str) -> dict[str, float]:
        """Send full setpoint to winner; redistribute overflow above max_power to others."""
        result: dict[str, float] = {
            sid: 0.0 for sid, _ in self.configs
        }
        winner_cfg = next(
            cfg for sid, cfg in self.configs if sid == winner
        )
        winner_state = self.states.get(winner)
        winner_soc_kwh = (
            winner_state.soc_kwh
            if winner_state is not None
            else (winner_cfg.min_soc_kwh + winner_cfg.max_soc_kwh) / 2
        )

        others = [
            (sid, cfg) for sid, cfg in self.configs if sid != winner
        ]

        def _hand_to_others(amount_kw: float) -> dict[str, float]:
            """Spread ``amount_kw`` over the non-winning batteries."""
            if others:
                result.update(self._proportional_split(amount_kw, others))
            return result

        if total_kw > 0:
            if winner_cfg.max_soc_kwh - winner_soc_kwh <= 0:
                # Winner is full (can happen via selection hysteresis): hand
                # the whole setpoint to the remaining batteries instead of
                # dropping it.
                return _hand_to_others(total_kw)
            clamped = min(total_kw, winner_cfg.max_charge_at_soc(winner_soc_kwh))
        else:
            if winner_soc_kwh - winner_cfg.min_soc_kwh <= 0:
                # Winner is empty: redistribute the discharge to the others.
                return _hand_to_others(total_kw)
            clamped = max(total_kw, -winner_cfg.max_discharge_at_soc(winner_soc_kwh))

        result[winner] = clamped
        overflow = total_kw - clamped
        if abs(overflow) > 1e-6:
            _hand_to_others(overflow)
        return result

    def _proportional_split(
        self,
        total_kw: float,
        configs: list[tuple[str, BatteryConfig]],
    ) -> dict[str, float]:
        """Proportional split with iterative overflow redistribution.

        Splits total_kw proportionally to available headroom (charging) or
        available energy (discharging). Batteries that hit their max_power
        limit have the overflow redistributed to the remaining batteries.
        Iterates at most len(configs) rounds until all overflow is absorbed
        or no capacity remains.
        """
        result: dict[str, float] = {}
        remaining_kw = total_kw
        remaining = list(configs)

        for _ in range(len(configs)):
            if not remaining or abs(remaining_kw) < 1e-6:
                break

            if remaining_kw > 0:
                weights = {
                    sid: max(
                        0.0,
                        cfg.max_soc_kwh - self.states[sid].soc_kwh,
                    )
                    if sid in self.states
                    else cfg.max_soc_kwh * 0.5
                    for sid, cfg in remaining
                }
            else:
                weights = {
                    sid: max(
                        0.0,
                        self.states[sid].soc_kwh - cfg.min_soc_kwh,
                    )
                    if sid in self.states
                    else cfg.capacity_kwh * 0.4
                    for sid, cfg in remaining
                }

            total_weight = sum(weights.values())
            if total_weight <= 0:
                # Every remaining battery is full (charging) or empty
                # (discharging), so there is nothing left to distribute to.
                # Spreading the setpoint evenly here instead — as this used to —
                # commanded a charge into a battery with no room: the weights
                # are exactly the headroom, and max_charge_at_soc only knows
                # about derating, so nothing downstream caught it.
                break

            overflow = 0.0
            next_remaining: list[tuple[str, BatteryConfig]] = []

            for sid, cfg in remaining:
                raw = remaining_kw * weights[sid] / total_weight
                state = self.states.get(sid)
                soc_kwh = (
                    state.soc_kwh
                    if state is not None
                    else (cfg.min_soc_kwh + cfg.max_soc_kwh) / 2
                )
                if remaining_kw > 0:
                    max_chg = cfg.max_charge_at_soc(soc_kwh)
                    clamped = min(raw, max_chg)
                    at_limit = clamped >= max_chg - 1e-6
                else:
                    max_dchg = cfg.max_discharge_at_soc(soc_kwh)
                    clamped = max(raw, -max_dchg)
                    at_limit = clamped <= -max_dchg + 1e-6

                result[sid] = result.get(sid, 0.0) + clamped
                overflow += raw - clamped
                if not at_limit:
                    next_remaining.append((sid, cfg))

            remaining_kw = overflow
            remaining = next_remaining

        for sid, _ in configs:
            result.setdefault(sid, 0.0)
        return result

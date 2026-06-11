#!/usr/bin/env python3
"""Brute-force optimizer analysis based on diagnostics.json.

Runs a grid of scenarios against the real optimizer and stores the results in CSV.
The goal is to reveal when small changes in start time / partial first step / SoC
cause the recommended charge setpoint to jump between discrete levels.
"""

from __future__ import annotations

import csv
import json
import os
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from custom_components.battery_controller.battery_model import BatteryConfig
from custom_components.battery_controller.optimizer import optimize_battery_schedule


@dataclass
class ScenarioResult:
    start_idx: int
    offset_min: int
    current_soc_kwh: float
    first_step_h: float
    optimal_power_kw: float
    optimal_mode: str
    step0_power_kw: float
    step0_mode: str
    step1_power_kw: float
    step1_mode: str
    shadow_price: float
    is_partial_step0: bool
    start_time: str


def load_inputs(path: str) -> tuple[BatteryConfig, dict, dict]:
    with open(path) as f:
        diag = json.load(f)

    bc = diag["data"]["battery_config"]
    battery = BatteryConfig(
        capacity_kwh=bc["capacity_kwh"],
        usable_capacity_kwh=bc["usable_capacity_kwh"],
        max_charge_power_kw=bc["max_charge_power_kw"],
        max_discharge_power_kw=bc["max_discharge_power_kw"],
        round_trip_efficiency=bc["round_trip_efficiency"],
        min_soc_percent=bc["min_soc_percent"],
        max_soc_percent=bc["max_soc_percent"],
        pv_dc_coupled=bc["pv_dc_coupled"],
        pv_dc_peak_power_kwp=bc["pv_dc_peak_power_kwp"],
        pv_dc_efficiency=bc["pv_dc_efficiency"],
        max_grid_power_kw=diag["data"]["config_entry"]["options"].get(
            "max_grid_power_kw", 0.0
        ),
    )
    return (
        battery,
        diag["data"]["optimization"],
        diag["data"]["config_entry"]["options"],
    )


def clamp_soc(soc_kwh: float, battery: BatteryConfig) -> float:
    return max(battery.min_soc_kwh, min(battery.max_soc_kwh, soc_kwh))


def slice_schedule(schedule: dict, start_idx: int, offset_min: int) -> dict:
    sliced: dict[str, list[float] | list[str]] = {}
    keys = [
        "price_forecast",
        "feed_in_price_forecast",
        "pv_forecast",
        "consumption_forecast",
        "step_durations_hours",
        "step_start_times_iso",
    ]
    for key in keys:
        sliced[key] = list(schedule[key][start_idx:])

    base_step_h = sliced["step_durations_hours"][0]
    if offset_min > 0:
        remaining_h = max(1.0 / 60.0, base_step_h - offset_min / 60.0)
        sliced["step_durations_hours"][0] = remaining_h
    return sliced


def run_scenarios(
    path: str,
    *,
    max_start_idx: int = 12,
    offsets: list[int] | None = None,
    soc_deltas: list[float] | None = None,
) -> list[ScenarioResult]:
    battery, optimization, options = load_inputs(path)
    schedule = optimization["schedule"]
    base_soc = optimization["battery_state"]["soc_kwh"]

    degradation_cost = float(options.get("degradation_cost_per_kwh", 0.04))
    min_price_spread = float(options.get("min_price_spread", 0.0))

    if soc_deltas is None:
        soc_deltas = [-0.20, -0.15, -0.10, -0.05, 0.0, 0.05, 0.10, 0.15, 0.20]
    if offsets is None:
        offsets = [0, 3, 12, 15, 27, 42, 57]

    start_indices = list(range(min(max_start_idx, len(schedule["price_forecast"]) - 2)))

    results: list[ScenarioResult] = []
    for start_idx in start_indices:
        for offset_min in offsets:
            sliced = slice_schedule(schedule, start_idx, offset_min)
            if len(sliced["price_forecast"]) < 2:
                continue
            for delta in soc_deltas:
                current_soc_kwh = clamp_soc(base_soc + delta, battery)
                result = optimize_battery_schedule(
                    battery_config=battery,
                    current_soc_kwh=current_soc_kwh,
                    price_forecast=sliced["price_forecast"],
                    feed_in_forecast=sliced["feed_in_price_forecast"],
                    pv_forecast=sliced["pv_forecast"],
                    consumption_forecast=sliced["consumption_forecast"],
                    step_durations_hours=sliced["step_durations_hours"],
                    degradation_cost_per_kwh=degradation_cost,
                    min_price_spread=min_price_spread,
                )
                first_step_h = float(sliced["step_durations_hours"][0])
                full_step_h = (
                    float(sliced["step_durations_hours"][1])
                    if len(sliced["step_durations_hours"]) > 1
                    else first_step_h
                )
                is_partial_step0 = first_step_h < full_step_h * 0.9
                results.append(
                    ScenarioResult(
                        start_idx=start_idx,
                        offset_min=offset_min,
                        current_soc_kwh=round(current_soc_kwh, 4),
                        first_step_h=round(first_step_h, 4),
                        optimal_power_kw=round(result.optimal_power_kw, 4),
                        optimal_mode=result.optimal_mode,
                        step0_power_kw=round(
                            result.power_schedule_kw[0]
                            if result.power_schedule_kw
                            else 0.0,
                            4,
                        ),
                        step0_mode=result.mode_schedule[0]
                        if result.mode_schedule
                        else "idle",
                        step1_power_kw=round(
                            result.power_schedule_kw[1]
                            if len(result.power_schedule_kw) > 1
                            else 0.0,
                            4,
                        ),
                        step1_mode=(
                            result.mode_schedule[1]
                            if len(result.mode_schedule) > 1
                            else "idle"
                        ),
                        shadow_price=round(result.shadow_price_eur_kwh, 4),
                        is_partial_step0=is_partial_step0,
                        start_time=sliced["step_start_times_iso"][0],
                    )
                )
    return results


def write_csv(path: str, results: list[ScenarioResult]) -> None:
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(asdict(results[0]).keys()))
        writer.writeheader()
        for row in results:
            writer.writerow(asdict(row))


def print_summary(results: list[ScenarioResult]) -> None:
    by_power = Counter((r.optimal_mode, r.optimal_power_kw) for r in results)
    print("Top recommendations:")
    for (mode, power), count in by_power.most_common(12):
        print(f"  {mode:12} {power:+5.2f} kW  -> {count:3d} scenarios")

    by_offset = defaultdict(Counter)
    for row in results:
        by_offset[row.offset_min][(row.optimal_mode, row.optimal_power_kw)] += 1

    print("\nPer offset:")
    for offset in sorted(by_offset):
        parts = []
        for (mode, power), count in by_offset[offset].most_common(5):
            parts.append(f"{mode}:{power:+.2f}={count}")
        print(f"  offset {offset:>2} min -> " + ", ".join(parts))

    # Partial-step scenarios (first step < 90% of full interval).
    # The optimizer now always uses step 0 directly; step 1 is no longer
    # used as a proxy, so optimal_power_kw == step0_power_kw for all runs.
    partial = [r for r in results if r.is_partial_step0]
    print(f"\nPartial-step scenarios: {len(partial)} / {len(results)}")

    by_start = defaultdict(set)
    for row in results:
        by_start[(row.start_idx, row.offset_min)].add(
            (row.optimal_mode, row.optimal_power_kw)
        )

    unstable_starts = [
        (start_idx, offset, sorted(values))
        for (start_idx, offset), values in by_start.items()
        if len(values) > 1
    ]
    print(
        f"Start windows with multiple outcomes across SoC variants: {len(unstable_starts)}"
    )
    for start_idx, offset, values in unstable_starts[:12]:
        rendered = ", ".join(f"{mode}:{power:+.2f}" for mode, power in values)
        print(f"  start_idx={start_idx:>2}, offset={offset:>2} -> {rendered}")


def main() -> int:
    input_path = (
        sys.argv[1]
        if len(sys.argv) > 1
        else os.path.join("simulate", "diagnostics.json")
    )
    output_path = (
        sys.argv[2]
        if len(sys.argv) > 2
        else os.path.join("simulate", "bruteforce_results.csv")
    )
    max_start_idx = int(sys.argv[3]) if len(sys.argv) > 3 else 12
    offsets = (
        [int(x) for x in sys.argv[4].split(",") if x.strip()]
        if len(sys.argv) > 4
        else None
    )
    soc_deltas = (
        [float(x) for x in sys.argv[5].split(",") if x.strip()]
        if len(sys.argv) > 5
        else None
    )
    results = run_scenarios(
        input_path,
        max_start_idx=max_start_idx,
        offsets=offsets,
        soc_deltas=soc_deltas,
    )
    if not results:
        print("No results produced.")
        return 1
    write_csv(output_path, results)
    print_summary(results)
    print(f"\nWrote {len(results)} scenario rows to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

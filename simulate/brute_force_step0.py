#!/usr/bin/env python3
"""
Brute-force investigation: why does the DP produce different first-step actions
across 15-minute runs during what should be a fixed discharge hour?

Varies two parameters independently:
  1. current_soc_kwh  — changes as battery discharges between runs
  2. step_durations_hours[0] — partial first step (decreases as we run mid-interval)

For each combination runs the DP, records step-0 action, saves to CSV,
and prints a summary table.

Usage:
    python brute_force_step0.py [diagnostics.json]
"""

import csv
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Reuse simulate_diagnostics internals
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).parent))
from simulate_diagnostics import (
    extract_inputs,
    load_diagnostics,
    run_dp,
    forward_pass,
)

# ---------------------------------------------------------------------------
# Sweep parameters
# ---------------------------------------------------------------------------

# SoC values to test (kWh)
SOC_VALUES = [round(v, 2) for v in [x * 0.5 for x in range(2, 22)]]  # 1.0 … 10.5 kWh

# First-step durations (hours): from 1 min to 15 min (full quarter)
STEP0_DURATIONS = [
    round(v, 4)
    for v in [1 / 60, 3 / 60, 5 / 60, 8 / 60, 10 / 60, 12 / 60, 14 / 60, 15 / 60]
]


def run_sweep(diag_path: str) -> list[dict]:
    diag = load_diagnostics(diag_path)
    (
        battery,
        base_soc_kwh,
        price_forecast,
        feed_in_forecast,
        pv_forecast,
        consumption_forecast,
        base_step_durations,
        step_start_times,
        degradation_cost,
        min_price_spread,
        fixed_feed_in_price,
        terminal_shadow_price,
        opt_data,
        sched,
    ) = extract_inputs(diag)

    n_steps = len(price_forecast)
    pv_dc_forecast = [0.0] * n_steps

    print(f"\nLoaded {diag_path}")
    print(f"  Base SoC={base_soc_kwh:.3f} kWh, steps={n_steps}")
    print(
        f"  Base step0 duration={base_step_durations[0]:.4f}h  ({base_step_durations[0] * 60:.1f} min)"
    )
    print(
        f"  Battery: cap={battery.max_soc_kwh:.1f} kWh, min={battery.min_soc_kwh:.2f} kWh"
    )
    print(f"  Max discharge: {battery.max_discharge_power_kw:.2f} kW")
    print()

    rows = []

    # Filter SoC range to battery limits
    soc_range = [
        s for s in SOC_VALUES if battery.min_soc_kwh <= s <= battery.max_soc_kwh
    ]
    # Also always include the base SoC
    if base_soc_kwh not in soc_range:
        soc_range.append(base_soc_kwh)
        soc_range.sort()

    total = len(soc_range) * len(STEP0_DURATIONS)
    done = 0

    for soc in soc_range:
        for step0_h in STEP0_DURATIONS:
            # Build modified step durations
            step_durations = list(base_step_durations)
            step_durations[0] = step0_h

            (
                V,
                policy,
                soc_states,
                soc_resolution_wh,
                power_step_w,
                step_durations_out,
                min_soc_wh,
                max_soc_wh,
                terminal_price,
            ) = run_dp(
                battery_config=battery,
                current_soc_kwh=soc,
                price_forecast=price_forecast,
                feed_in_forecast=feed_in_forecast,
                pv_forecast=pv_forecast,
                consumption_forecast=consumption_forecast,
                step_durations_hours=step_durations,
                degradation_cost_per_kwh=degradation_cost,
                min_price_spread=min_price_spread,
                pv_dc_forecast=pv_dc_forecast,
                terminal_shadow_price=terminal_shadow_price,
            )

            power_schedule, mode_schedule, soc_schedule = forward_pass(
                policy=policy,
                soc_states=soc_states,
                current_soc_kwh=soc,
                step_durations_hours=step_durations_out,
                min_soc_wh=min_soc_wh,
                max_soc_wh=max_soc_wh,
                n_steps=n_steps,
                battery_config=battery,
                pv_dc_forecast=pv_dc_forecast,
            )

            step0_action_w = power_schedule[0] * 1000
            step1_action_w = (
                power_schedule[1] * 1000 if len(power_schedule) > 1 else 0.0
            )
            step2_action_w = (
                power_schedule[2] * 1000 if len(power_schedule) > 2 else 0.0
            )
            step0_mode = mode_schedule[0]

            # Energy that can be discharged at max power in step0
            max_energy_wh = battery.max_discharge_power_kw * 1000 * step0_h
            soc_above_min_wh = (soc - battery.min_soc_kwh) * 1000

            rows.append(
                {
                    "soc_kwh": round(soc, 3),
                    "step0_h": round(step0_h, 4),
                    "step0_min": round(step0_h * 60, 1),
                    "step0_action_w": step0_action_w,
                    "step0_action_kw": round(step0_action_w / 1000, 2),
                    "step0_mode": step0_mode,
                    "step1_action_kw": round(step1_action_w / 1000, 2),
                    "step2_action_kw": round(step2_action_w / 1000, 2),
                    "soc_above_min_wh": round(soc_above_min_wh, 1),
                    "max_energy_step0_wh": round(max_energy_wh, 1),
                    "power_step_w": power_step_w,
                    "terminal_price": round(terminal_price, 4),
                }
            )

            done += 1
            if done % 20 == 0:
                print(f"  {done}/{total} runs done...", end="\r")

    print(f"  {total}/{total} runs complete.     ")
    return rows, battery, base_soc_kwh, base_step_durations


def print_table(rows, battery, base_soc_kwh, base_step_durations):
    """Print a 2D table: rows=SoC, columns=step0 duration, cell=step0 action kW."""
    soc_values = sorted({r["soc_kwh"] for r in rows})
    step_values = sorted({r["step0_min"] for r in rows})

    # Map (soc, step0_min) -> action_kw
    lookup = {(r["soc_kwh"], r["step0_min"]): r["step0_action_kw"] for r in rows}

    print()
    print("=" * 90)
    print("  STEP-0 ACTION (kW) vs SoC × First-Step Duration")
    print(
        f"  Base SoC={base_soc_kwh:.2f} kWh, Base step0={base_step_durations[0] * 60:.1f} min"
    )
    print("  Negative = discharging, 0 = idle, Positive = charging")
    print("=" * 90)

    # Header
    header = f"{'SoC (kWh)':>11}  {'Avail Wh':>8} |"
    for m in step_values:
        header += f" {m:>5.1f}m"
    print(header)
    print("-" * 90)

    for soc in soc_values:
        avail = round((soc - battery.min_soc_kwh) * 1000, 0)
        # Mark base SoC
        soc_marker = " *" if abs(soc - base_soc_kwh) < 0.01 else "  "
        row = f"{soc:>9.2f}{soc_marker}  {avail:>8.0f} |"
        for m in step_values:
            val = lookup.get((soc, m), float("nan"))
            row += f" {val:>6.2f}"
        print(row)

    print("-" * 90)
    print("  * = base SoC from diagnostics.json")
    print()


def print_action_analysis(rows, battery):
    """Show which (SoC, step0) combinations produce max power vs reduced power."""
    max_discharge_kw = -battery.max_discharge_power_kw

    not_max = [
        r
        for r in rows
        if r["step0_mode"] == "discharging" and r["step0_action_kw"] > max_discharge_kw
    ]
    at_max = [r for r in rows if r["step0_action_kw"] <= max_discharge_kw]
    idle = [r for r in rows if r["step0_mode"] != "discharging"]

    print(f"  Runs at max discharge ({max_discharge_kw:.2f} kW): {len(at_max)}")
    print(f"  Runs discharging but below max:                {len(not_max)}")
    print(f"  Runs idle/charging:                            {len(idle)}")
    print()

    if not_max:
        print("  Cases where step0 discharge is BELOW max power:")
        print(
            f"  {'SoC':>8}  {'step0_min':>9}  {'action_kw':>9}  {'step1_kw':>8}  {'step2_kw':>8}  {'avail_wh':>8}  {'max_E_wh':>8}"
        )
        print(
            f"  {'-' * 8}  {'-' * 9}  {'-' * 9}  {'-' * 8}  {'-' * 8}  {'-' * 8}  {'-' * 8}"
        )
        for r in sorted(not_max, key=lambda x: (x["soc_kwh"], x["step0_min"])):
            print(
                f"  {r['soc_kwh']:>8.2f}  {r['step0_min']:>9.1f}  "
                f"{r['step0_action_kw']:>9.2f}  {r['step1_action_kw']:>8.2f}  "
                f"{r['step2_action_kw']:>8.2f}  {r['soc_above_min_wh']:>8.0f}  "
                f"{r['max_energy_step0_wh']:>8.0f}"
            )

    print()


def save_csv(rows, path="brute_force_results.csv"):
    if not rows:
        return
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=rows[0].keys())
        w.writeheader()
        w.writerows(rows)
    print(f"  Saved {len(rows)} rows to {path}")


def main():
    diag_path = sys.argv[1] if len(sys.argv) > 1 else "diagnostics.json"

    rows, battery, base_soc_kwh, base_step_durations = run_sweep(diag_path)

    print_table(rows, battery, base_soc_kwh, base_step_durations)
    print_action_analysis(rows, battery)
    save_csv(rows)

    # Also show: for the base SoC, how does action change with step0 duration?
    base_rows = sorted(
        [r for r in rows if abs(r["soc_kwh"] - base_soc_kwh) < 0.01],
        key=lambda x: x["step0_min"],
    )
    if base_rows:
        print(f"  Effect of step0 duration at base SoC={base_soc_kwh:.2f} kWh:")
        print(f"  {'step0_min':>9}  {'action_kw':>9}  {'mode':>12}  {'step1_kw':>8}")
        for r in base_rows:
            print(
                f"  {r['step0_min']:>9.1f}  {r['step0_action_kw']:>9.2f}  {r['step0_mode']:>12}  {r['step1_action_kw']:>8.2f}"
            )


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Compare HA optimizer (with efficiency in SoC transitions) vs simulator (without).

Diagnoses why HA doesn't schedule today's charge/discharge cycle.
If diagnostics include `control_action`, this script also shows the published
controller target next to the raw optimizer recommendation.
"""

import json
import math
import sys
import os

# Add parent directory so we can import the actual optimizer
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from custom_components.battery_controller.optimizer import (
    optimize_battery_schedule,
    calculate_step_cost,
)
from custom_components.battery_controller.battery_model import BatteryConfig


def load_inputs(path):
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
    )

    sched = diag["data"]["optimization"]["schedule"]
    options = diag["data"]["config_entry"]["options"]

    current_soc_kwh = diag["data"]["optimization"]["battery_state"]["soc_kwh"]
    price_forecast = sched["price_forecast"]
    feed_in_forecast = sched.get("feed_in_price_forecast", list(price_forecast))
    pv_forecast = sched.get("pv_forecast_kw", sched.get("pv_forecast"))
    consumption_forecast = sched.get(
        "consumption_forecast_kw", sched.get("consumption_forecast")
    )
    step_durations_hours = sched.get("step_durations_hours")
    step_start_times = sched.get("step_start_times_iso", [])
    degradation_cost = options.get("degradation_cost_per_kwh", 0.04)
    min_price_spread = options.get("min_price_spread", 0.0)
    terminal_shadow_price = sched.get("terminal_shadow_price")

    control_action = diag["data"]["optimization"].get("control_action", {})

    return (
        battery,
        current_soc_kwh,
        price_forecast,
        feed_in_forecast,
        pv_forecast,
        consumption_forecast,
        step_durations_hours,
        step_start_times,
        degradation_cost,
        min_price_spread,
        terminal_shadow_price,
        control_action,
    )


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else "diagnostics.json"
    (
        battery,
        current_soc_kwh,
        price_forecast,
        feed_in_forecast,
        pv_forecast,
        consumption_forecast,
        step_durations_hours,
        step_start_times,
        degradation_cost,
        min_price_spread,
        terminal_shadow_price,
        control_action,
    ) = load_inputs(path)

    sqrt_rte = math.sqrt(battery.round_trip_efficiency)

    print(
        f"Battery: {battery.capacity_kwh} kWh, RTE={battery.round_trip_efficiency}, √RTE={sqrt_rte:.4f}"
    )
    print(
        f"Current SoC: {current_soc_kwh:.4f} kWh ({current_soc_kwh / battery.max_soc_kwh * 100:.1f}%)"
    )
    print(
        f"Min SoC: {battery.min_soc_kwh:.4f} kWh, Max SoC: {battery.max_soc_kwh:.4f} kWh"
    )
    print(
        f"Degradation: {degradation_cost:.4f} €/kWh, Min spread: {min_price_spread:.4f}"
    )
    print(f"Terminal shadow price: {terminal_shadow_price}")
    if control_action:
        print(
            "Published control action: "
            f"{control_action.get('action_mode', 'unknown')} @ "
            f"{control_action.get('target_power_kw', 0.0):.4f} kW"
        )
    print()

    # Run the ACTUAL HA optimizer
    result = optimize_battery_schedule(
        battery_config=battery,
        current_soc_kwh=current_soc_kwh,
        price_forecast=price_forecast,
        feed_in_forecast=feed_in_forecast,
        pv_forecast=pv_forecast,
        consumption_forecast=consumption_forecast,
        step_durations_hours=step_durations_hours,
        degradation_cost_per_kwh=degradation_cost,
        min_price_spread=min_price_spread,
    )

    print("=" * 120)
    print(f"  HA OPTIMIZER RESULT  (shadow_price={result.shadow_price_eur_kwh:.4f})")
    print("=" * 120)

    breakeven_discharge = (terminal_shadow_price + degradation_cost) / sqrt_rte
    breakeven_charge = (terminal_shadow_price - degradation_cost) * sqrt_rte

    print(f"  Break-even discharge: {breakeven_discharge:.4f} €/kWh")
    print(f"  Break-even charge:    {breakeven_charge:.4f} €/kWh")
    print()

    header = f"{'Step':>4}  {'Time':>11}  {'Price':>7}  {'FeedIn':>7}  {'PV':>6}  {'Cons':>6}  {'NetLoad':>8}  {'Power':>7}  {'Mode':>12}  {'SoC':>8}"
    print(header)
    print("-" * 110)

    n = len(result.power_schedule_kw)
    for t in range(n):
        time_str = (
            step_start_times[t][5:16].replace("T", " ")
            if t < len(step_start_times)
            else f"step {t:02d}"
        )
        price = price_forecast[t]
        feed_in = feed_in_forecast[t] if t < len(feed_in_forecast) else price
        pv = pv_forecast[t] if t < len(pv_forecast) else 0
        cons = consumption_forecast[t] if t < len(consumption_forecast) else 0
        net_load = cons - pv
        power = result.power_schedule_kw[t]
        mode = result.mode_schedule[t]
        soc = result.soc_schedule_kwh[t]

        marker = ""
        if mode in ("charging", "discharging"):
            marker = " ◄"

        row = f"{t:>4}  {time_str:>11}  {price:>7.4f}  {feed_in:>7.4f}  {pv:>6.3f}  {cons:>6.3f}  {net_load:>+8.3f}  {power:>+7.3f}  {mode:>12}  {soc:>8.4f}{marker}"
        print(row)

    print(f"\nSavings: {result.savings:.4f} €")
    print(f"Total cost: {result.total_cost:.4f} €")
    print(f"Baseline cost: {result.baseline_cost:.4f} €")
    if result.raw_total_cost is not None:
        print(f"Raw total cost (DP): {result.raw_total_cost:.4f} €")
        print(f"Raw savings (DP): {result.raw_savings:.4f} €")
        filter_impact = (result.raw_savings or 0) - result.savings
        print(f"Filter impact: {filter_impact:+.4f} € (raw_savings - savings)")

    # Now analyze specific profitable steps
    print("\n" + "=" * 80)
    print("  PROFITABILITY ANALYSIS PER STEP")
    print("=" * 80)

    for t in range(n):
        price = price_forecast[t]
        feed_in = feed_in_forecast[t] if t < len(feed_in_forecast) else price
        pv = pv_forecast[t] if t < len(pv_forecast) else 0
        cons = consumption_forecast[t] if t < len(consumption_forecast) else 0
        pv_surplus = pv - cons

        # Effective charge price (opportunity cost)
        eff_charge = feed_in if pv_surplus > 0.05 else price

        discharge_profit = price * sqrt_rte - terminal_shadow_price - degradation_cost
        charge_profit = terminal_shadow_price - eff_charge / sqrt_rte - degradation_cost

        if discharge_profit > 0 or charge_profit > 0:
            time_str = (
                step_start_times[t][5:16].replace("T", " ")
                if t < len(step_start_times)
                else f"step {t:02d}"
            )
            actual = result.mode_schedule[t]
            flags = []
            if discharge_profit > 0:
                flags.append(
                    f"DISCHARGE_PROFIT={discharge_profit * 100:.2f}ct/kWh (price={price:.4f})"
                )
            if charge_profit > 0:
                flags.append(
                    f"CHARGE_PROFIT={charge_profit * 100:.2f}ct/kWh (eff_price={eff_charge:.4f}, pv_surplus={pv_surplus:.3f})"
                )
            print(
                f"  Step {t:>2} ({time_str}): actual={actual:>12}  {', '.join(flags)}"
            )

    # Check what the SoC states look like for current position
    from custom_components.battery_controller.const import SOC_RESOLUTION_WH

    soc_res = float(SOC_RESOLUTION_WH)
    min_soc_wh = round(battery.min_soc_kwh * 1000)
    max_soc_wh = round(battery.max_soc_kwh * 1000)
    n_states = int(round((max_soc_wh - min_soc_wh) / soc_res)) + 1

    current_soc_wh = int(current_soc_kwh * 1000)
    current_idx = round((current_soc_wh - min_soc_wh) / soc_res)

    print(f"\n  SoC discretization: {soc_res} Wh, {n_states} states")
    print(
        f"  Current SoC: {current_soc_wh} Wh → state index {current_idx} = {min_soc_wh + current_idx * soc_res:.0f} Wh"
    )

    # Check feasibility of key actions at current SoC
    soc_state = min_soc_wh + current_idx * soc_res
    print(f"\n  From SoC state {soc_state:.0f} Wh:")
    print(f"    Headroom for charging: {max_soc_wh - soc_state:.0f} Wh")
    print(f"    Headroom for discharging: {soc_state - min_soc_wh:.0f} Wh")

    # Max discharge feasibility
    available = soc_state - min_soc_wh
    max_discharge_w = available / (
        1.0 / sqrt_rte
    )  # available / (1/sqrt_rte) = available * sqrt_rte
    print(
        f"    Max discharge (1h, with eff): {max_discharge_w:.0f}W AC (draws {available:.0f} Wh from DC)"
    )
    print(f"    Max discharge (1h, no eff):   {available:.0f}W AC (simulator thinks)")

    # What would happen if we charge at step 5-6, discharge at 11-12?
    # Use the simulator's plan: 600W then 1200W charge, 500W then 1200W discharge
    print("\n" + "=" * 80)
    print("  HYPOTHETICAL A: Simulator's plan (600+1200 charge, 500+1200 discharge)")
    print("=" * 80)

    soc = soc_state
    steps_plan = [
        (5, 600, "charge"),
        (6, 1200, "charge"),
        (11, -500, "discharge"),
        (12, -1200, "discharge"),
    ]

    total_extra_cost = 0.0
    for t, action_w, label in steps_plan:
        dt = step_durations_hours[t]
        price = price_forecast[t]
        feed_in = feed_in_forecast[t]
        pv_w = pv_forecast[t] * 1000
        cons_w = consumption_forecast[t] * 1000

        # SoC feasibility
        if action_w > 0:
            delta_soc = action_w * dt * sqrt_rte
            new_soc = soc + delta_soc
            feasible = new_soc <= max_soc_wh
        else:
            delta_soc = abs(action_w) * dt / sqrt_rte
            new_soc = soc - delta_soc
            feasible = new_soc >= min_soc_wh

        # Cost comparison
        cost_action = calculate_step_cost(
            dt,
            soc,
            action_w,
            price,
            feed_in,
            pv_w,
            cons_w,
            battery.round_trip_efficiency,
            degradation_cost,
            battery,
        )
        cost_idle = calculate_step_cost(
            dt,
            soc,
            0,
            price,
            feed_in,
            pv_w,
            cons_w,
            battery.round_trip_efficiency,
            degradation_cost,
            battery,
        )
        extra = cost_action - cost_idle
        total_extra_cost += extra

        print(
            f"  Step {t:>2}: {label:>10} {action_w:>+5d}W  SoC {soc:.0f} → {new_soc:.0f} Wh  "
            f"{'OK' if feasible else 'INFEASIBLE':>10}  "
            f"cost_diff={extra:>+.4f}€  (action={cost_action:.4f}, idle={cost_idle:.4f})"
        )

        if feasible and action_w > 0:
            soc = new_soc
        elif feasible and action_w < 0:
            soc = new_soc
        else:
            print("         *** STOP: action is infeasible at this SoC! ***")
            break

    print(
        f"\n  Total extra cost of cycle: {total_extra_cost:+.4f} € ({total_extra_cost * 100:+.2f} ct)"
    )
    if total_extra_cost < 0:
        print(f"  → PROFITABLE: saves {-total_extra_cost * 100:.2f} ct")
    else:
        print(
            f"  → NOT PROFITABLE: costs {total_extra_cost * 100:.2f} ct more than idle"
        )

    # Try optimal: charge max at cheapest, discharge max feasible at peak
    print("\n" + "=" * 80)
    print(
        "  HYPOTHETICAL B: Optimal feasible cycle (charge 5-6, discharge at max feasible 11-12)"
    )
    print("=" * 80)

    # Find the best feasible plan
    for plan_label, plan_steps in [
        (
            "1200+700 charge, 1200+max discharge",
            [
                (5, 1200, "charge"),
                (6, 700, "charge"),
                (11, -1200, "discharge"),
                (12, "max", "discharge"),
            ],
        ),
        (
            "600+1200 charge, 500+max discharge",
            [
                (5, 600, "charge"),
                (6, 1200, "charge"),
                (11, -500, "discharge"),
                (12, "max", "discharge"),
            ],
        ),
        (
            "1200 charge step6, 1200 discharge step12",
            [(6, 1200, "charge"), (12, -1200, "discharge")],
        ),
        (
            "1200 charge step6, 1000 discharge step12",
            [(6, 1200, "charge"), (12, -1000, "discharge")],
        ),
        (
            "1200 charge step6, 900 discharge step11",
            [(6, 1200, "charge"), (11, -900, "discharge")],
        ),
    ]:
        soc = soc_state
        total_extra_cost = 0.0
        all_feasible = True

        for t, action_w_or_max, label in plan_steps:
            dt = step_durations_hours[t]
            price = price_forecast[t]
            feed_in = feed_in_forecast[t]
            pv_w = pv_forecast[t] * 1000
            cons_w = consumption_forecast[t] * 1000

            if action_w_or_max == "max":
                max_draw = soc - min_soc_wh
                max_action = max_draw * sqrt_rte
                pstep = max(100.0, 25.0 / dt)  # POWER_STEP_W floor, SOC_RES / step_h
                action_w = -int(max_action / pstep) * pstep
            else:
                action_w = action_w_or_max

            if action_w > 0:
                delta_soc = action_w * dt * sqrt_rte
                new_soc = soc + delta_soc
                feasible = new_soc <= max_soc_wh
            else:
                delta_soc = abs(action_w) * dt / sqrt_rte
                new_soc = soc - delta_soc
                feasible = new_soc >= min_soc_wh

            cost_action = calculate_step_cost(
                dt,
                soc,
                action_w,
                price,
                feed_in,
                pv_w,
                cons_w,
                battery.round_trip_efficiency,
                degradation_cost,
                battery,
            )
            cost_idle = calculate_step_cost(
                dt,
                soc,
                0,
                price,
                feed_in,
                pv_w,
                cons_w,
                battery.round_trip_efficiency,
                degradation_cost,
                battery,
            )
            extra = cost_action - cost_idle
            total_extra_cost += extra

            if feasible:
                soc = new_soc
            else:
                all_feasible = False
                break

        status = "FEASIBLE" if all_feasible else "INFEASIBLE"
        profit = (
            "PROFITABLE" if total_extra_cost < 0 and all_feasible else "NOT PROFITABLE"
        )
        print(
            f"  {plan_label:>55}: {status:>10}  cost={total_extra_cost:+.4f}€ ({total_extra_cost * 100:+.2f}ct)  {profit}"
        )


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Verify: does the cost model correctly compute grid costs?

The optimizer splits RTE as √RTE on both charge and discharge sides.
This means action_w is neither AC power nor DC power - it's the geometric mean.
This test checks whether that creates a bias in the economics.
"""

import math
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from custom_components.battery_controller.optimizer import calculate_step_cost
from custom_components.battery_controller.battery_model import BatteryConfig

battery = BatteryConfig(
    capacity_kwh=2.12,
    max_charge_power_kw=1.2,
    max_discharge_power_kw=1.2,
    round_trip_efficiency=0.76,
    min_soc_percent=12.0,
    max_soc_percent=100.0,
)

RTE = 0.76
SQRT_RTE = math.sqrt(RTE)
DEG = 0.0215

print(f"RTE = {RTE}, √RTE = {SQRT_RTE:.4f}")
print()

# ============================================================
# Scenario 1: Charge from PV surplus
# PV=2716W, Cons=550W, Price=0.1998 (feed-in = grid price)
# ============================================================
print("=" * 80)
print("SCENARIO 1: Charge 1200W during PV surplus")
print("  PV=2716W, Cons=550W, Price=€0.1998")
print("=" * 80)

price = 0.1998
pv = 2716.0
cons = 550.0

# What the code computes
cost_charge = calculate_step_cost(
    1.0, 1000, 1200, price, price, pv, cons, RTE, DEG, battery
)
cost_idle = calculate_step_cost(1.0, 1000, 0, price, price, pv, cons, RTE, DEG, battery)

print("\n  Code model (action_w = geometric mean of AC and DC):")
print(f"    grid_to_battery = action_w / √RTE = {1200 / SQRT_RTE:.0f}W")
print(
    f"    net_grid (charge) = {cons} - {pv} + {1200 / SQRT_RTE:.0f} = {cons - pv + 1200 / SQRT_RTE:.0f}W"
)
print(f"    net_grid (idle)   = {cons} - {pv} = {cons - pv:.0f}W")
print(f"    cost_charge = {cost_charge:.4f}€, cost_idle = {cost_idle:.4f}€")
print(f"    Opportunity cost of charging = {(cost_charge - cost_idle) * 100:.2f} ct")

# What reality would be (action_w = AC power input to inverter)
# Battery stores: 1200 × √RTE Wh. Grid draws: 1200W (the AC input).
real_net_grid_charge = cons - pv + 1200  # grid draws exactly 1200W
real_net_grid_idle = cons - pv

if real_net_grid_charge < 0:
    real_cost_charge = -abs(real_net_grid_charge) / 1000 * price
else:
    real_cost_charge = real_net_grid_charge / 1000 * price
real_cost_charge += 1200 * SQRT_RTE / 1000 * DEG  # degradation on stored energy

real_cost_idle = -abs(real_net_grid_idle) / 1000 * price

print("\n  Physical reality (action_w = AC power, grid draws 1200W):")
print("    grid_to_battery = 1200W (AC input = what grid meter sees)")
print(f"    net_grid (charge) = {cons} - {pv} + 1200 = {real_net_grid_charge:.0f}W")
print(f"    cost_charge = {real_cost_charge:.4f}€, cost_idle = {real_cost_idle:.4f}€")
print(
    f"    Opportunity cost of charging = {(real_cost_charge - real_cost_idle) * 100:.2f} ct"
)

bias = (cost_charge - cost_idle) - (real_cost_charge - real_cost_idle)
print(f"\n  *** BIAS: code overcharges by {bias * 100:.2f} ct per hour ***")
print(f"      Source: phantom {1200 / SQRT_RTE - 1200:.0f}W extra grid draw")

# ============================================================
# Scenario 2: Discharge at high price
# PV=996W, Cons=800W, Price=0.3723
# ============================================================
print()
print("=" * 80)
print("SCENARIO 2: Discharge 1200W at peak price")
print("  PV=996W, Cons=800W, Price=€0.3723")
print("=" * 80)

price2 = 0.3723
pv2 = 996.0
cons2 = 800.0

cost_discharge = calculate_step_cost(
    1.0, 1500, -1200, price2, price2, pv2, cons2, RTE, DEG, battery
)
cost_idle2 = calculate_step_cost(
    1.0, 1500, 0, price2, price2, pv2, cons2, RTE, DEG, battery
)

print("\n  Code model:")
print(f"    usable_power = action_w × √RTE = {1200 * SQRT_RTE:.0f}W delivered to AC")
print(
    f"    net_grid (discharge) = {cons2} - {pv2} - {1200 * SQRT_RTE:.0f} = {cons2 - pv2 - 1200 * SQRT_RTE:.0f}W"
)
print(f"    net_grid (idle)      = {cons2} - {pv2} = {cons2 - pv2:.0f}W")
print(f"    cost_discharge = {cost_discharge:.4f}€, cost_idle = {cost_idle2:.4f}€")
print(f"    Revenue from discharge = {(cost_idle2 - cost_discharge) * 100:.2f} ct")

# Reality: battery delivers 1200W to AC
real_net_grid_discharge = cons2 - pv2 - 1200
real_cost_discharge = (
    -abs(real_net_grid_discharge) / 1000 * price2
    if real_net_grid_discharge < 0
    else real_net_grid_discharge / 1000 * price2
)
real_cost_discharge += 1200 / SQRT_RTE / 1000 * DEG  # degradation on DC-side energy

print("\n  Physical reality (battery delivers 1200W AC):")
print(
    f"    net_grid (discharge) = {cons2} - {pv2} - 1200 = {real_net_grid_discharge:.0f}W"
)
print(f"    cost_discharge = {real_cost_discharge:.4f}€")
print(f"    Revenue from discharge = {(cost_idle2 - real_cost_discharge) * 100:.2f} ct")

bias2 = (cost_idle2 - cost_discharge) - (cost_idle2 - real_cost_discharge)
print(f"\n  *** BIAS: code undervalues discharge by {-bias2 * 100:.2f} ct per hour ***")
print(f"      Source: phantom {1200 - 1200 * SQRT_RTE:.0f}W less AC delivery")

# ============================================================
# Total cycle bias
# ============================================================
print()
print("=" * 80)
print("TOTAL CYCLE BIAS (charge + discharge)")
print("=" * 80)

code_cycle_profit = -(cost_charge - cost_idle) - (
    cost_discharge - cost_idle2
)  # sign: charge costs, discharge earns
real_cycle_profit = -(real_cost_charge - real_cost_idle) - (
    real_cost_discharge - cost_idle2
)

print(f"  Code cycle profit:  {code_cycle_profit * 100:+.2f} ct")
print(f"  Real cycle profit:  {real_cycle_profit * 100:+.2f} ct")
print(f"  Bias against cycling: {(real_cycle_profit - code_cycle_profit) * 100:.2f} ct")
print(
    f"  Code underestimates profit by factor {real_cycle_profit / code_cycle_profit:.1f}x"
)

# ============================================================
# How big is the bias for different RTE values?
# ============================================================
print()
print("=" * 80)
print("BIAS VS RTE (at same prices, 1200W charge/discharge)")
print("=" * 80)
print(
    f"  {'RTE':>5}  {'√RTE':>6}  {'Phantom charge W':>16}  {'Phantom disch W':>16}  {'Total bias ct':>14}"
)
for rte in [0.70, 0.76, 0.80, 0.85, 0.90, 0.95, 1.00]:
    sr = math.sqrt(rte)
    phantom_charge = 1200 / sr - 1200  # extra grid draw the code claims
    phantom_discharge = 1200 - 1200 * sr  # less AC delivery the code claims
    bias_charge = phantom_charge / 1000 * 0.20  # at charge price
    bias_discharge = phantom_discharge / 1000 * 0.37  # at discharge price
    total_bias = (bias_charge + bias_discharge) * 100
    print(
        f"  {rte:>5.2f}  {sr:>6.4f}  {phantom_charge:>+16.0f}  {phantom_discharge:>+16.0f}  {total_bias:>+14.2f}"
    )

print()
print("  → At RTE=0.76, the bias is ~9ct per full-power cycle hour.")
print("    For a 2kWh battery cycling 1.86kWh, that's about 14ct bias per full cycle.")
print(
    "    The actual profit of today's cycle is ~15ct → bias makes it appear break-even!"
)

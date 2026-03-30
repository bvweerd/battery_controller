import json
import math
from dataclasses import dataclass

# Mock constants
SOC_RESOLUTION_WH = 25.0
DC_TO_AC_INVERTER_EFFICIENCY = 0.96


@dataclass
class BatteryConfig:
    capacity_kwh: float
    min_soc_kwh: float
    max_soc_kwh: float
    max_charge_power_kw: float
    max_discharge_power_kw: float
    round_trip_efficiency: float
    pv_dc_efficiency: float = 0.97
    pv_dc_coupled: bool = False


def _find_nearest_soc_idx(soc_wh, soc_states):
    if len(soc_states) <= 1:
        return 0
    step = soc_states[1] - soc_states[0]
    idx = round((soc_wh - soc_states[0]) / step)
    return max(0, min(idx, len(soc_states) - 1))


def calculate_step_cost(
    time_step_hours,
    soc_wh,
    action_w,
    grid_price,
    feed_in_price,
    pv_production_w,
    consumption_w,
    rte,
    degradation_cost_per_kwh,
    battery_config,
):
    sqrt_rte = math.sqrt(rte)
    if action_w > 0:  # CHARGING
        grid_to_battery_w = action_w / sqrt_rte
    elif action_w < 0:  # DISCHARGING
        grid_to_battery_w = -abs(action_w) * sqrt_rte
    else:
        grid_to_battery_w = 0.0

    net_grid_w = consumption_w - pv_production_w + grid_to_battery_w
    energy_kwh = abs(net_grid_w) * time_step_hours / 1000
    if net_grid_w > 0:
        grid_cost = energy_kwh * grid_price
    else:
        grid_cost = -energy_kwh * feed_in_price

    throughput_kwh = abs(action_w) * time_step_hours / 1000
    degradation_cost = throughput_kwh * degradation_cost_per_kwh
    return grid_cost + degradation_cost


def optimize(battery_config, current_soc_kwh, prices, pv, cons, time_step_min):
    time_step_hours = time_step_min / 60.0
    soc_resolution_wh = float(SOC_RESOLUTION_WH)
    power_step_w = soc_resolution_wh / time_step_hours
    min_soc_wh = battery_config.min_soc_kwh * 1000
    max_soc_wh = battery_config.max_soc_kwh * 1000
    n_soc_states = int(round((max_soc_wh - min_soc_wh) / soc_resolution_wh)) + 1
    soc_states = [min_soc_wh + i * soc_resolution_wh for i in range(n_soc_states)]

    n_steps = len(prices)
    V = [[float("inf")] * n_soc_states for _ in range(n_steps + 1)]
    policy = [[0.0] * n_soc_states for _ in range(n_steps)]

    terminal_price = prices[-1]
    for s_idx, soc_wh in enumerate(soc_states):
        stored_kwh = (soc_wh - min_soc_wh) / 1000.0
        V[n_steps][s_idx] = -stored_kwh * terminal_price

    actions = [
        float(i * power_step_w)
        for i in range(
            int(battery_config.max_charge_power_kw * 1000 / power_step_w) + 1
        )
    ]
    actions += [
        float(-i * power_step_w)
        for i in range(
            1, int(battery_config.max_discharge_power_kw * 1000 / power_step_w) + 1
        )
    ]

    for t in range(n_steps - 1, -1, -1):
        for s_idx, soc_wh in enumerate(soc_states):
            for action_w in actions:
                new_soc_wh = soc_wh + action_w * time_step_hours
                if new_soc_wh < min_soc_wh - 0.01 or new_soc_wh > max_soc_wh + 0.01:
                    continue

                new_soc_idx = _find_nearest_soc_idx(new_soc_wh, soc_states)
                step_cost = calculate_step_cost(
                    time_step_hours,
                    soc_wh,
                    action_w,
                    prices[t],
                    prices[t],
                    pv[t] * 1000,
                    cons[t] * 1000,
                    battery_config.round_trip_efficiency,
                    0.04,
                    battery_config,
                )
                total_cost = step_cost + V[t + 1][new_soc_idx]
                if total_cost < V[t][s_idx]:
                    V[t][s_idx] = total_cost
                    policy[t][s_idx] = action_w

    # Extraction
    curr_soc = current_soc_kwh * 1000
    schedule = []
    for t in range(n_steps):
        s_idx = _find_nearest_soc_idx(curr_soc, soc_states)
        action = policy[t][s_idx]
        schedule.append(action)
        curr_soc += action * time_step_hours
    return schedule


# Data from diagnostics
config = BatteryConfig(
    capacity_kwh=2.12,
    min_soc_kwh=0.2544,
    max_soc_kwh=2.12,
    max_charge_power_kw=1.2,
    max_discharge_power_kw=1.2,
    round_trip_efficiency=0.76,
)
with open("/media/data/github/battery_controller/diagnostics.json") as f:
    diag = json.load(f)

prices = diag["data"]["optimization"]["schedule"]["price_forecast"]
pv = diag["data"]["optimization"]["schedule"]["pv_forecast"]
cons = diag["data"]["optimization"]["schedule"]["consumption_forecast"]

# Test 5 min (original)
print("Testing 5 min...")
sched_5 = optimize(config, 0.2544, prices[:100], pv[:100], cons[:100], 5)
print(f"Non-zero steps in first 100 (5 min): {sum(1 for x in sched_5 if x != 0)}")

# Test 15 min (resampled)
print("Testing 15 min...")
prices_15 = [prices[i] for i in range(0, len(prices), 3)]
pv_15 = [pv[i] for i in range(0, len(pv), 3)]
cons_15 = [cons[i] for i in range(0, len(cons), 3)]
sched_15 = optimize(config, 0.2544, prices_15[:100], pv_15[:100], cons_15[:100], 15)
print(f"Non-zero steps in first 100 (15 min): {sum(1 for x in sched_15 if x != 0)}")

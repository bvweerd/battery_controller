# Battery Controller

**Home battery cost optimization for Home Assistant**

Minimize electricity costs by intelligently scheduling battery charge and discharge cycles using dynamic programming, price forecasts, PV production, and consumption patterns.

[![GitHub Release](https://img.shields.io/github/release/bvweerd/battery_controller.svg?style=flat-square)](https://github.com/bvweerd/battery_controller/releases)
[![License](https://img.shields.io/github/license/bvweerd/battery_controller.svg?style=flat-square)](LICENSE)
[![hacs](https://img.shields.io/badge/HACS-Default-orange.svg?style=flat-square)](https://hacs.xyz)

---

## What is Battery Controller?

This Home Assistant custom integration optimizes your home battery to minimize electricity costs. It uses **dynamic programming** (backward induction) to calculate the optimal charge/discharge schedule based on:

- **Electricity price forecasts** (Nordpool, ENTSO-E, or any price sensor with forecast attributes)
- **PV production forecasts** (from open-meteo.com solar radiation data)
- **Household consumption patterns** (learned from historical DSMR energy data)
- **Battery characteristics** (capacity, power limits, round-trip efficiency, degradation)

### Key Features

- **Price arbitrage**: Charge during cheap hours, discharge during expensive hours
- **PV self-consumption**: Maximize use of solar energy, minimize feed-in
- **Multi-array PV**: Up to 3 PV arrays with independent orientation/tilt (e.g. south + east + west)
- **DC-coupled PV support**: Higher efficiency for panels directly on the battery inverter's DC bus (hybrid inverters)
- **DSMR energy sensors**: Learn consumption patterns from your smart meter's cumulative kWh sensors
- **Zero-grid control**: Real-time battery control to minimize grid exchange
- **Degradation-aware**: Accounts for battery wear in optimization decisions
- **Multiple control modes**: Zero-grid, follow schedule, hybrid, or manual

## How It Works

### Architecture

The integration runs three cascading coordinators:

1. **Weather Coordinator** (every 30 min): Fetches solar radiation forecasts from open-meteo.com
2. **Forecast Coordinator** (every 15 min): Calculates PV production and consumption forecasts
3. **Optimization Coordinator** (every 15 min): Runs the DP optimizer and zero-grid controller

### Dynamic Programming Optimizer

The core algorithm uses **backward induction** (dynamic programming) to find the cost-minimal battery schedule:

**1. State Space**
- Time steps: 0 to T (e.g., 96 steps for 24 hours at 15-minute resolution)
- SoC states: Discretized in 100 Wh steps (e.g., 100 states for a 10 kWh battery)
- Power actions: Discretized in 500 W steps (charge/discharge/idle)

**2. Cost Function** (per time step)
- **Grid cost**: `price × (consumption - PV + battery_losses)`
- **Degradation cost**: `€0.03/kWh × battery_throughput`
- **Feed-in revenue**: `feed_in_price × exported_energy` (when negative)

The optimizer compares:
- Export PV surplus now vs store for later
- Import from grid vs discharge battery
- Battery cycling cost (RTE + degradation) vs price spread

**3. Bellman Equation**

```
V[t][s] = min over actions (
    step_cost(t, s, action) + V[t+1][s']
)
```

Where:
- `V[t][s]` = minimum cost from time `t` to end, starting at SoC state `s`
- `s'` = new SoC after applying `action`
- Terminal condition: `V[T][s] = 0` for all states

**4. Algorithm Steps**

**Backward pass** (planning):
```python
for t in range(T-1, -1, -1):  # From end to start
    for each SoC state s:
        for each action (charge/discharge/idle):
            cost = immediate_cost + future_cost[next_state]
            if cost < best_cost:
                best_action[t][s] = action
```

**Forward pass** (execution):
```python
for t in range(0, T):
    action = best_action[t][current_soc_state]
    execute(action)
    current_soc = new_soc_after_action
```

**5. Key Decisions**

The optimizer automatically handles:
- **Price arbitrage**: Charge during cheap hours (€0.05), discharge during expensive hours (€0.30)
- **Feed-in optimization**:
  - High feed-in (€0.10) + low future prices → Export now (don't store)
  - Low feed-in (€0.04) + high future prices (€0.30) → Store for later
- **Self-consumption**: Store PV surplus when grid import is expensive
- **Degradation awareness**: Only cycles battery when price spread justifies wear cost

### Efficiency Model

**Round-Trip Efficiency (RTE)**
- Split symmetrically: `charge_eff = discharge_eff = sqrt(RTE)`
- For 90% RTE: each direction has ~94.9% efficiency
- This ensures `charge_eff × discharge_eff = RTE`

**DC-Coupled PV** (hybrid inverters)
- **DC charge path**: PV → MPPT → Battery (~97% efficient)
- **AC charge path**: PV → Inverter → AC → Charger → Battery (~85% efficient)
- **Excess DC PV**: Converted to AC through inverter (~96% efficient)
- Optimizer prefers DC charging when available (higher efficiency = lower cost)

## Installation

### HACS (Recommended)

1. Open HACS in Home Assistant
2. Go to "Integrations" -> "+" -> Search "Battery Controller"
3. Download and restart Home Assistant

### Manual

1. Copy `custom_components/battery_controller` to your `custom_components` directory
2. Restart Home Assistant
3. Go to **Settings -> Devices & Services -> Add Integration -> Battery Controller**

## Configuration

Configuration is done through a single form with collapsible sections.

### Battery

| Parameter | Default | Description |
|-----------|---------|-------------|
| Capacity (kWh) | 10.0 | Total battery capacity |
| Max charge power (kW) | 5.0 | Maximum charging power |
| Max discharge power (kW) | 5.0 | Maximum discharging power |
| Round-trip efficiency | 0.90 | Round-trip efficiency (0-1) |

### Sensors (required)

| Parameter | Description |
|-----------|-------------|
| Electricity price sensor | Price sensor with forecast attributes (Nordpool, ENTSO-E, etc.) |
| Battery SoC sensor | Battery state-of-charge sensor (% or kWh) |

### PV Array 1 (optional, collapsed)

| Parameter | Default | Description |
|-----------|---------|-------------|
| Peak power AC PV (kWp) | 0.0 | AC-coupled PV system size |
| Orientation (degrees) | 180 | Panel orientation (180=south) |
| Tilt (degrees) | 35 | Panel tilt angle |
| System efficiency | 0.85 | Overall AC PV system efficiency |
| DC-coupled PV | false | Enable DC-coupled PV |
| Peak power DC PV (kWp) | 0.0 | DC-coupled PV system size |
| DC coupling efficiency | 0.97 | DC PV charge efficiency |

### PV Array 2 & 3 (optional, collapsed)

Additional PV arrays with independent orientation and tilt. Use these for east/west split installations.

| Parameter | Default (array 2) | Default (array 3) |
|-----------|-------------------|-------------------|
| Peak power (kWp) | 0.0 | 0.0 |
| Orientation (degrees) | 90 (east) | 270 (west) |
| Tilt (degrees) | 35 | 35 |

### Optional Sensors (collapsed)

| Parameter | Description |
|-----------|-------------|
| Feed-in price sensor | Separate feed-in/export price sensor |
| Battery power sensor | Real-time battery power (W) |
| Electricity consumption sensors | One or more DSMR cumulative kWh sensors for consumption (e.g. tariff 1 + tariff 2) |
| Electricity production sensors | One or more DSMR cumulative kWh sensors for production |

The consumption and production sensors are used to learn your household's consumption pattern from historical data. These are typically the cumulative kWh sensors created by a DSMR smart meter integration (e.g. `sensor.electricity_consumed_tariff_1`, `sensor.electricity_consumed_tariff_2`).

### Advanced (collapsed)

| Parameter | Default | Description |
|-----------|---------|-------------|
| Time step (minutes) | 15 | Optimization resolution |
| Optimization interval (minutes) | 15 | How often to re-optimize |
| Fixed feed-in price (EUR/kWh) | 0.07 | Fallback feed-in price when no sensor configured |
| Zero-grid enabled | true | Enable zero-grid control mode |

## Entities Created

### Sensors (12)

| Entity | Unit | Description | Attributes |
|--------|------|-------------|------------|
| Optimal Power | kW | Recommended battery power | `optimal_mode`, `current_price` |
| Optimal Mode | — | Current mode: `charging`, `discharging`, `idle`, `zero_grid`, `manual` | — |
| **Schedule** | — | Full optimization schedule (see below) | `power_schedule_kw`, `mode_schedule`, `soc_schedule_kwh`, `price_forecast` |
| State of Charge | % | Current battery SoC | `soc_kwh`, `power_kw`, `mode` |
| Battery Power | kW | Current battery power | — |
| PV Forecast | kW | Current PV production | `forecast_kw`, `dc_forecast_kw`\*, `current_dc_pv_kw`\* |
| Consumption Forecast | kW | Current consumption estimate | `forecast_kw` |
| Net Grid Forecast | kW | Net grid power (positive=import) | `forecast_kw` |
| Estimated Savings | EUR | Cost savings from optimization | `baseline_cost`, `optimized_cost` |
| Grid Setpoint | W | Target grid power (zero-grid) | Full control action dict |
| Control Mode | — | Current control mode | — |
| Optimization Status | — | Optimizer health (`ok`/`error`/`waiting`) | `n_steps`, `total_cost`, `baseline_cost`, `savings`, `current_price`, `timestamp` |

\* Only present when DC-coupled PV is configured.

All `forecast_kw` attributes are lists at **optimizer step resolution** (default: 15 minutes). Step 0 = current, step 1 = next step, etc. The PV, Consumption, and Net Grid sensors each have their own `forecast_kw`, all at the same resolution and time alignment as the Schedule sensor.

### Schedule Sensor (detail)

The **Schedule** sensor is the core output of the optimizer. Its state shows a summary like `C:4 D:6 I:14` (4 charging, 6 discharging, 14 idle steps). All data is in the attributes:

| Attribute | Type | Description |
|-----------|------|-------------|
| `power_schedule_kw` | `list[float]` | Battery power for each time step. Positive = charge, negative = discharge. Length equals the number of planning steps (e.g. 96 for 24h at 15-min resolution). |
| `mode_schedule` | `list[str]` | Mode per step: `"charging"`, `"discharging"`, or `"idle"`. Same length as `power_schedule_kw`. |
| `soc_schedule_kwh` | `list[float]` | Predicted state-of-charge (kWh) at the start of each step. |
| `price_forecast` | `list[float]` | Electricity buy price (EUR/kWh) used for each step. |

All lists share the same length and time alignment. Step 0 = current time step, step 1 = next time step, etc. The time step duration is configured in Advanced settings (default: 15 minutes).

**Example usage in a template:**

```yaml
# Current scheduled power
{{ state_attr('sensor.battery_controller_schedule', 'power_schedule_kw')[0] }}

# Price in 2 hours (step 8 at 15-min resolution)
{{ state_attr('sensor.battery_controller_schedule', 'price_forecast')[8] }}
```

**Example ApexCharts card:**

```yaml
type: custom:apexcharts-card
header:
  title: Battery Schedule
span:
  start: hour
series:
  - entity: sensor.battery_controller_schedule
    data_generator: |
      const schedule = entity.attributes.power_schedule_kw;
      const now = new Date();
      return schedule.map((val, i) => {
        const t = new Date(now.getTime() + i * 15 * 60000);
        return [t.getTime(), val];
      });
    name: Power (kW)
```

### Number Entities (5)

| Entity | Range | Description |
|--------|-------|-------------|
| Minimum SoC | 0-50% | Runtime adjustable min SoC |
| Maximum SoC | 50-100% | Runtime adjustable max SoC |
| Degradation Cost | 0-0.20 EUR/kWh | Battery wear cost per kWh throughput |
| Min Price Spread | 0-0.50 EUR/kWh | Minimum price spread to trigger arbitrage |
| Zero Grid Deadband | 0-500 W | Deadband for zero-grid mode |

### Select Entity

| Entity | Options | Description |
|--------|---------|-------------|
| Control Mode | zero_grid, follow_schedule, hybrid, manual | Battery control strategy |

### Switch Entity

| Entity | Description |
|--------|-------------|
| Optimization Enabled | Enable/disable the optimizer |

## Control Modes

- **Zero Grid**: Minimize grid exchange in real-time using the battery
- **Follow Schedule**: Execute the DP-optimized schedule exactly
- **Hybrid** (recommended): DP schedule for price arbitrage (charge/discharge), zero-grid for self-consumption during idle periods
- **Manual**: No automatic control

## Controlling Your Battery

Battery Controller calculates the optimal schedule but does **not** directly control your inverter. You need an automation that reads the `Optimal Mode` and `Optimal Power` sensors and sends the corresponding commands to your inverter integration.

### Key Sensors for Control

| Sensor | Values | Purpose |
|--------|--------|---------|
| `sensor.battery_controller_optimal_mode` | `charging`, `discharging`, `idle`, `zero_grid`, `manual` | What the battery should do now |
| `sensor.battery_controller_optimal_power` | float (kW) | How much power (positive=charge, negative=discharge) |

The `optimal_mode` reflects the active **control mode**:

| Control Mode | Optimal Mode | Optimal Power | Behavior |
|-------------|-------------|---------------|----------|
| `follow_schedule` | `charging` / `discharging` / `idle` | From DP schedule | Execute the optimized schedule exactly |
| `hybrid` | `charging` / `discharging` / `zero_grid` | From DP schedule (0 for zero_grid) | Arbitrage from schedule, self-consumption during idle |
| `zero_grid` | `zero_grid` | `0.0` | Inverter handles self-consumption |
| `manual` | `manual` | `0.0` | No automatic control |

The raw DP schedule is always available via the **Schedule** sensor attributes, regardless of control mode.

### Example Automation

This automation watches the optimal mode and sets the inverter accordingly. Adapt the entity IDs and service calls to your specific inverter integration.

```yaml
automation:
  - alias: "Battery Controller - Follow optimal schedule"
    description: "Set inverter mode and power based on optimizer output"
    trigger:
      - platform: state
        entity_id: sensor.battery_controller_optimal_mode
      - platform: state
        entity_id: sensor.battery_controller_optimal_power
    action:
      - choose:
          # Charging
          - conditions:
              - condition: state
                entity_id: sensor.battery_controller_optimal_mode
                state: "charging"
            sequence:
              - service: number.set_value
                target:
                  entity_id: number.YOUR_INVERTER_charge_power
                data:
                  value: >
                    {{ (states('sensor.battery_controller_optimal_power') | float * 1000) | round(0) }}
              - service: select.select_option
                target:
                  entity_id: select.YOUR_INVERTER_mode
                data:
                  option: "Force Charge"

          # Discharging
          - conditions:
              - condition: state
                entity_id: sensor.battery_controller_optimal_mode
                state: "discharging"
            sequence:
              - service: number.set_value
                target:
                  entity_id: number.YOUR_INVERTER_discharge_power
                data:
                  value: >
                    {{ (states('sensor.battery_controller_optimal_power') | float | abs * 1000) | round(0) }}
              - service: select.select_option
                target:
                  entity_id: select.YOUR_INVERTER_mode
                data:
                  option: "Force Discharge"

          # Zero-grid: let inverter handle self-consumption
          - conditions:
              - condition: state
                entity_id: sensor.battery_controller_optimal_mode
                state: "zero_grid"
            sequence:
              - service: select.select_option
                target:
                  entity_id: select.YOUR_INVERTER_mode
                data:
                  option: "Maximize Self Consumption"

          # Idle or Manual: set inverter to auto/standby
        default:
          - service: select.select_option
            target:
              entity_id: select.YOUR_INVERTER_mode
            data:
              option: "Auto"
```

### Inverter Integration

Replace `YOUR_INVERTER` with your actual inverter entity names. Most battery inverters expose:
- A **mode select entity** to control operation mode (charge/discharge/auto)
- A **power number entity** to set charge/discharge power limits

Check your inverter's Home Assistant integration documentation for the correct entity names and available modes.

**Common entity patterns**:
- Mode: `select.{inverter}_mode`, `select.{inverter}_operation_mode`, `select.{inverter}_working_mode`
- Power: `number.{inverter}_charge_power`, `number.{inverter}_discharge_power`, `number.{inverter}_max_power`

> **Tip**: The `idle`, `manual`, and `zero_grid` modes all use the `default` branch. If you need different inverter behavior for each, replace the `default` with separate `choose` conditions.

> **Tip**: Set `select.battery_controller_control_mode` to `manual` to temporarily disable the automation and take manual control of your inverter.

## Prerequisites

- A dynamic electricity price sensor with forecast attributes (e.g., Nordpool, ENTSO-E, or [Dynamic Energy Contract Calculator](https://github.com/bvweerd/dynamic_energy_contract_calculator))
- A battery SoC sensor from your inverter integration

## Troubleshooting

Enable debug logging:

```yaml
logger:
  default: info
  logs:
    custom_components.battery_controller: debug
```

## License

MIT License - see [LICENSE](LICENSE) for details.

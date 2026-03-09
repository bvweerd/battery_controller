# Battery Controller

**Home battery cost optimization for Home Assistant**

[![GitHub Release](https://img.shields.io/github/release/bvweerd/battery_controller.svg?style=flat-square)](https://github.com/bvweerd/battery_controller/releases)
[![License](https://img.shields.io/github/license/bvweerd/battery_controller.svg?style=flat-square)](LICENSE)
[![hacs](https://img.shields.io/badge/HACS-Default-orange.svg?style=flat-square)](https://hacs.xyz)

---

## What is Battery Controller?

This Home Assistant custom integration optimizes your home battery to minimize electricity costs. It uses **dynamic programming** (backward induction) to calculate the optimal charge/discharge schedule based on:

- **Electricity price forecasts** (Nordpool, ENTSO-E, or any price sensor with forecast attributes like the [Dynamic Energy Contract Calculator](https://github.com/bvweerd/dynamic_energy_contract_calculator))
- **PV production forecasts** (from open-meteo.com solar radiation data)
- **Household consumption patterns** (learned from historical DSMR energy data)
- **Battery characteristics** (capacity, power limits, round-trip efficiency, degradation)
- **Historical price model** (fallback and horizon extension when day-ahead prices are not yet published)

Battery Controller works with any battery inverter and electricity meter — it is a calculated integration that reads sensors and writes setpoints. It does not communicate directly with hardware.

### Key Features

- **Price arbitrage**: Charge during cheap hours, discharge during expensive hours
- **Multi-battery support**: Configure multiple batteries with independent sensors; the optimizer treats them as one aggregate while distributing setpoints proportionally.
- **Multi-array PV**: Add any number of PV arrays with independent orientation/tilt (e.g. south + east + west)
- **Pre-day-ahead price model**: Optimize before day-ahead prices are published (typically before 13:00 CET) using a self-learning historical model that improves over time
- **Horizon extension**: When live day-ahead prices cover less than 24 hours, the remaining hours are filled with the historical model automatically
- **PV self-consumption**: Maximize use of solar energy, minimize feed-in
- **DC-coupled PV support**: Higher efficiency for panels directly on the battery inverter's DC bus (hybrid inverters)
- **Zero-grid control**: Real-time battery control to minimize grid exchange
- **Degradation-aware**: Accounts for battery wear in optimization decisions
- **Multiple control modes**: Zero-grid, follow schedule, hybrid, or manual

---

## How It Works

### Architecture

The integration runs three cascading coordinators:

1. **Weather Coordinator** (every 30 min): Fetches solar radiation and wind speed forecasts from open-meteo.com
2. **Forecast Coordinator** (every 15 min): Calculates PV production and consumption forecasts
3. **Optimization Coordinator** (every 15 min): Runs the DP optimizer and zero-grid controller

#### Subentry Structure
Battery Controller uses **subentries** to manage hardware flexibly:
- **Battery subentries**: Each contains its own capacity, power limits, SoC sensor, and optional power sensor.
- **PV Array subentries**: Each contains its own peak power, orientation, tilt, and coupling type.

The optimizer aggregates all battery subentries into a single virtual battery for planning. When executing the schedule, the required power is split across the physical batteries proportional to their available headroom (charging) or stored energy (discharging).

### Historical Price Model (Pre-Day-Ahead Fallback)

Day-ahead electricity prices (Nordpool, ENTSO-E) are published around 13:00 CET. Before that, the integration uses a **self-learning historical price model** so the optimizer can still run on a reasonable forecast.

The model also **extends the planning horizon** when live prices cover less than 24 hours. It builds lookup tables from data in the HA recorder using hour, weekday, solar irradiance (GHI), and wind speed.

---

## Installation

### Prerequisites

- A dynamic electricity price sensor with forecast attributes (e.g., Nordpool, ENTSO-E, or the [Dynamic Energy Contract Calculator](https://github.com/bvweerd/dynamic_energy_contract_calculator)).
- Battery SoC sensor(s) from your inverter integration.
- [HACS](https://hacs.xyz/) installed in your Home Assistant (recommended).

### Via HACS (Recommended)

1. Navigate to HACS -> Integrations -> Three dots -> Custom repositories.
2. Add `https://github.com/bvweerd/battery_controller` as an **Integration**.
3. Install the "Battery Controller" integration and restart Home Assistant.

### Manual Installation

1. Copy `custom_components/battery_controller` to your `custom_components` directory.
2. Restart Home Assistant.

### Setup

After installation, add the integration through the UI:
**Settings → Devices & Services → Add Integration → Battery Controller**

Once the main integration is added, you MUST add your hardware as subentries:
1. Go to **Settings → Devices & Services → Battery Controller**.
2. Click **Add Subentry**.
3. Select **Battery** or **PV Array** and follow the instructions.

---

## Configuration

### Main Integration
The main configuration covers global sensors and advanced settings.

| Parameter | Description |
|-----------|-------------|
| Electricity price sensor | Price sensor with forecast attributes |
| Feed-in price sensor (opt) | Separate feed-in/export price sensor |
| Power consumption sensors | DSMR **power** sensors (W) for real-time grid balance |
| Power production sensors | DSMR **power** sensors (W) for real-time grid balance |
| Energy consumption sensors | DSMR cumulative kWh sensors for pattern learning |
| Energy production sensors | DSMR cumulative kWh sensors for pattern learning |

### Battery Subentry
| Parameter | Default | Description |
|-----------|---------|-------------|
| Capacity (kWh) | 10.0 | Total battery capacity |
| Max charge/discharge (kW) | 5.0 | Power limits |
| Round-trip efficiency | 0.90 | Battery efficiency (0-1) |
| Min/Max SoC (%) | 10.0 / 90.0 | Operating range for optimization |
| SoC sensor | — | State-of-charge sensor (% or kWh) |
| Power sensor (opt) | — | Real-time battery power sensor (kW) |

---

## Entities Created

### Sensors

| Entity | Unit | Description |
|--------|------|-------------|
| **Optimal Power** | W | **Strategy**: Battery power from DP optimizer (15-min planning). |
| Optimal Mode | — | Current mode: `charging`, `discharging`, `idle`, `zero_grid`, `manual` |
| **Schedule** | — | Full optimization schedule (available in attributes) |
| State of Charge | % | Combined battery SoC (weighted average) |
| Battery Power | kW | Combined battery power |
| **Battery Setpoint** | W | **Tactics**: Combined real-time power target (~5s updates). |
| **Battery Setpoint [Name]** | W | Per-battery power setpoint (split from combined setpoint). |
| **Shadow Price of Storage** | EUR/kWh | **Marginal value of 1 kWh stored right now**. |
| Estimated Savings | EUR | Financial impact of battery actions vs. doing nothing. |

**Convention**: All power sensors in this integration use **positive for discharge** and **negative for charge**.

### Number Entities

| Entity | Range | Default | Description |
|--------|-------|---------|-------------|
| Degradation Cost | 0-0.20 EUR/kWh | 0.03 | Battery wear cost per kWh throughput |
| **Min Price Spread** | 0-0.50 EUR/kWh | 0.05 | Minimum price spread to trigger arbitrage. |
| Zero Grid Deadband | 0-500 W | 50 W | Deadband for zero-grid mode |
| **Manual Power Setpoint** | — | 0 W | Target power in `manual` mode (+ = discharge) |

---

## Control Modes

- **Zero Grid**: Minimize grid exchange in real-time using the battery.
- **Follow Schedule**: Execute the DP-optimized schedule exactly.
- **Hybrid** (recommended): DP schedule for arbitrage, zero-grid for self-consumption.
- **Manual**: Target power set via `number.battery_controller_manual_power_setpoint`.

---

## Controlling Your Battery

Use an automation to read the `Optimal Mode` and `Battery Setpoint` (or `Optimal Power`) sensors and send commands to your inverter.

| Control Mode | Optimal Mode | Power Sensor to Use |
|-------------|-------------|---------------------|
| `follow_schedule` | `charging` / `discharging` | `sensor.battery_controller_optimal_power` (W) |
| `hybrid` / `zero_grid`| `charging` / `discharging` / `zero_grid` | `sensor.battery_controller_battery_setpoint` (W) |

### Example Automation

```yaml
automation:
  - alias: "Battery Controller - Inverter Control"
    trigger:
      - platform: state
        entity_id: sensor.battery_controller_optimal_mode
      - platform: state
        entity_id: sensor.battery_controller_battery_setpoint
    action:
      - variables:
          mode: "{{ states('sensor.battery_controller_optimal_mode') }}"
          power_w: "{{ states('sensor.battery_controller_battery_setpoint') | float }}"
      - choose:
          - conditions: "{{ power_w < -50 }}" # Charging
            sequence:
              - service: number.set_value
                target: { entity_id: number.inverter_charge_power }
                data: { value: "{{ power_w | abs }}" }
          - conditions: "{{ power_w > 50 }}" # Discharging
            sequence:
              - service: number.set_value
                target: { entity_id: number.inverter_discharge_power }
                data: { value: "{{ power_w }}" }
        default:
          - service: select.select_option
            target: { entity_id: select.inverter_mode }
            data: { option: "Auto" }
```

---

## License

MIT License — see [LICENSE](LICENSE) for details.

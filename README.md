# Battery Controller

**Home battery cost optimization for Home Assistant**

[![GitHub Release](https://img.shields.io/github/release/bvweerd/battery_controller.svg?style=flat-square)](https://github.com/bvweerd/battery_controller/releases)
[![License](https://img.shields.io/github/license/bvweerd/battery_controller.svg?style=flat-square)](LICENSE)
[![hacs](https://img.shields.io/badge/HACS-Default-orange.svg?style=flat-square)](https://hacs.xyz)

---

> **⚠️ This integration is aimed at technically experienced users.**
> It requires setting up and tuning a number of parameters (efficiency, degradation costs, price sensors, control mode) and connecting it to your inverter via automations. Incorrect configuration will not damage your battery, but may result in suboptimal scheduling or no control at all. If you are not comfortable reading diagnostics data and interpreting optimizer output, this integration may not be the right fit — yet.

---

## 🔍 Diagnostics Analyzer

**Upload your `diagnostics.json` to the online analyzer for a full breakdown of your configuration, optimizer schedule, profitability analysis, and improvement tips — no installation required.**

**[▶ Open Battery Controller Analyzer](https://bvweerd.github.io/battery_controller/)**

[![Battery Controller Analyzer screenshot](assets/analyzer.png)](https://bvweerd.github.io/battery_controller/)

The analyzer runs entirely in your browser. It visualizes the current schedule, re-runs the DP optimizer with your data, and explains every charge/discharge decision. To generate a diagnostics file: **Settings → Devices & Services → Battery Controller → three-dot menu → Download diagnostics**.

---

## What is Battery Controller?

> **For a detailed, step-by-step explanation of the optimization algorithm**, see [ALGORITHM.md](ALGORITHM.md).


This Home Assistant custom integration optimizes your home battery to minimize electricity costs. It uses **dynamic programming** (backward induction) to calculate the optimal charge/discharge schedule based on:

- **Electricity price forecasts** (Nordpool, ENTSO-E, or any price sensor with forecast attributes like the [Dynamic Energy Contract Calculator](https://github.com/bvweerd/dynamic_energy_contract_calculator))
- **PV production forecasts** (from open-meteo.com solar radiation data)
- **Household consumption patterns** (learned from historical energy meter data)
- **Battery characteristics** (capacity, power limits, round-trip efficiency, degradation)
- **Historical price model** (fallback and horizon extension when day-ahead prices are not yet published)

Battery Controller works with any battery inverter and electricity meter — it is a calculated integration that reads sensors and writes setpoints. It does not communicate directly with hardware.

![Battery Controller dashboard showing electricity prices, battery charge/discharge schedule and SoC](assets/battery-with-prediction.png)

### Key Features

- **Price arbitrage**: Charge during cheap hours, discharge during expensive hours
- **Multi-battery support**: Configure multiple batteries with independent sensors; the optimizer treats them as one aggregate while distributing setpoints proportionally
- **Multi-array PV**: Add any number of PV arrays with independent orientation/tilt (e.g. south + east + west)
- **Pre-day-ahead price model**: Optimize before day-ahead prices are published (typically before 13:00 CET) using a self-learning historical model that improves over time
- **Horizon extension**: When live day-ahead prices cover less than 24 hours, the remaining hours are filled with the historical model automatically
- **PV self-consumption**: Maximize use of solar energy, minimize feed-in
- **DC-coupled PV support**: Higher efficiency for panels directly on the battery inverter's DC bus (hybrid inverters)
- **Zero-grid control**: Real-time battery control to minimize grid exchange
- **Degradation-aware**: Accounts for battery wear in optimization decisions
- **Multiple control modes**: Zero-grid, follow schedule, hybrid, hybrid+, or manual
- **Negative price handling**: Suggests PV curtailment or maximum power consumption during negative prices

---

## How It Works

### Architecture

The integration runs three cascading coordinators:

1. **Weather Coordinator** (every 30 min): Fetches solar radiation and wind speed forecasts from open-meteo.com
2. **Forecast Coordinator** (every 15 min): Calculates PV production and consumption forecasts
3. **Optimization Coordinator** (every 15 min): Runs the DP optimizer and zero-grid controller (see [ALGORITHM.md](ALGORITHM.md) for full algorithmic details)

> **Note:** The Optimization Coordinator doesn't only run on the fixed 15-minute clock.
> It also re-runs immediately when a new price period starts, on a significant price
> change, when a stale price/SoC sensor becomes available again, and once at the
> midpoint of the current price period (a scheduled "mid-period correction" run). Each
> run re-solves the entire rolling horizon from scratch, so two schedule snapshots
> taken a few minutes apart can look meaningfully different — see [Learning
> period](#learning-period--give-the-optimizer-time-to-calibrate) for why.

#### Subentry Structure
Battery Controller uses **subentries** to manage hardware flexibly:
- **Battery subentries**: Each contains its own capacity, power limits, SoC sensor, and optional power sensor.
- **PV Array subentries**: Each contains its own peak power, orientation, tilt, and coupling type.

The optimizer aggregates all battery subentries into a single virtual battery for planning. When executing the schedule, the required power is split across the physical batteries proportional to their available headroom (charging) or stored energy (discharging).

### Historical Price Model (Pre-Day-Ahead Fallback)

Day-ahead electricity prices (Nordpool, ENTSO-E) are published around 13:00 CET. Before that, the integration uses a **self-learning historical price model** so the optimizer can still run on a reasonable forecast.

The model also **extends the planning horizon** when live prices cover less than 24 hours. It builds lookup tables from data in the HA recorder using hour, weekday, solar irradiance (GHI), and wind speed.

### Learning period — give the optimizer time to calibrate

> **Allow at least 2–4 weeks of operation before judging the optimizer's performance.**

The optimizer uses *rolling-horizon dynamic programming*: on **every** run it re-solves the entire planning horizon (24–36 hours) from scratch, starting from the current SoC. The value of stored energy at the end of the horizon (the terminal condition) is set from the price forecast itself — a clipped tail-average of the feed-in price — not carried over from the previous run.

Because battery capacity is limited, this is a **global allocation problem**: the DP weighs using the current cheap/negative-price window now against reserving capacity for a *better* opportunity later in the horizon. On a rerun, small input changes can shift that trade-off enough to visibly change today's plan:

- A price forecast update (day-ahead prices publishing, a revised historical-model estimate, or simply a new period starting).
- An updated PV or consumption forecast.
- The actual SoC drifting from what the previous plan assumed — e.g. because **Hybrid** mode diverged to zero-grid instead of following the DP schedule exactly (see [Control Modes](#control-modes)).

Because the whole horizon is re-optimized rather than patched incrementally, even a modest change in one of these inputs can shift how much capacity is allocated to the current window versus a later one — so two runs a few minutes apart (see the re-optimization triggers above) can show different schedules for what looks like the same situation. This is expected DP behavior, not a bug.

This effect is most visible in the first weeks, while two data-driven models are still building up from HA recorder history:

- The **household consumption pattern** and **historical price model**, which need time to learn typical usage and price patterns.
- The **shadow price** (λ) — the marginal value of storage, used by Hybrid mode as its charge/discharge threshold — is noisier while the forecasts feeding into it are still inaccurate.

During this convergence period you may notice:

- The schedule changing more noticeably between runs, including between two runs within the same 15-minute slot.
- The optimizer occasionally charging or discharging more aggressively than expected.
- Estimated savings that appear lower than the long-run optimum.

The longer the integration runs, the more accurate the underlying forecasts become and the more stable the resulting schedule.

---

## Installation

### Prerequisites

- **Home Assistant 2025.1 or later**
- A dynamic electricity price sensor with forecast attributes (e.g., Nordpool, ENTSO-E, or the [Dynamic Energy Contract Calculator](https://github.com/bvweerd/dynamic_energy_contract_calculator)).
- Battery SoC sensor(s) from your inverter integration.
- [HACS](https://hacs.xyz/) installed in your Home Assistant (recommended).

### Verifying your price sensor

The integration reads forecast data from the **forecast attributes** of your price sensor. Before setting up, verify that your sensor exposes the required attributes in Home Assistant's Developer Tools.

Go to **Developer Tools → States**, find your price sensor (e.g. `sensor.nordpool_kwh_nl_eur_3_10_21`) and check that the attributes contain a list of future prices. The integration supports several common formats:

**Nordpool / ENTSO-E style** — attributes contain `raw_today` and `raw_tomorrow`, each a list of objects with a `value` key:
```yaml
raw_today:
  - start: "2026-03-16T00:00:00+01:00"
    end:   "2026-03-16T01:00:00+01:00"
    value: 0.1234
  - ...
raw_tomorrow:
  - start: "2026-03-17T00:00:00+01:00"
    value: 0.2345
  - ...
```

**Generic forecast list** — attributes contain a `forecast` key with a list of objects:
```yaml
forecast:
  - datetime: "2026-03-16T12:00:00+00:00"
    price: 0.1580
  - datetime: "2026-03-16T13:00:00+00:00"
    price: 0.2210
  - ...
```

**Dynamic Energy Contract Calculator** — exposes `prices_today` and `prices_tomorrow` as plain lists of floats (one per hour), or a combined `price_forecast` list.

If your sensor's state is the current price but its attributes contain no forecast list, the optimizer will run with a flat price forecast and cannot perform meaningful arbitrage. In that case use a different sensor or add the [Dynamic Energy Contract Calculator](https://github.com/bvweerd/dynamic_energy_contract_calculator) on top of your existing sensor.

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

The main configuration covers global sensors and advanced settings, organised in collapsible sections.

**Sensors (required)**

| Parameter | Description |
|-----------|-------------|
| Electricity price sensor | Price sensor with forecast attributes |

**Optional Sensors**

| Parameter | Description |
|-----------|-------------|
| Feed-in price sensor | Separate feed-in/export price sensor |
| Power consumption sensors | Real-time grid import power sensors (W) for zero-grid control |
| Power production sensors | Real-time grid export power sensors (W) for zero-grid control |
| Energy consumption sensors | Cumulative kWh sensors for consumption pattern learning |
| Energy production sensors | Cumulative kWh sensors for production pattern learning |
| PV production sensors | Cumulative kWh sensors from PV inverters (used to reconstruct gross consumption) |

**Advanced**

| Parameter | Default | Description |
|-----------|---------|-------------|
| Optimization interval | 15 min | How often the DP optimizer runs |
| Fixed feed-in price | €0.04/kWh | Fallback feed-in price when no sensor is available |
| Zero grid enabled | true | Enable real-time zero-grid balance control |
| Zero grid response time | 10 s | Expected battery response delay; limits setpoint update rate |
| Max grid power | 0 kW | Grid connection cap (0 = unlimited) |

### Battery Subentry

| Parameter | Default | Description |
|-----------|---------|-------------|
| Name (opt) | — | Display name for this battery |
| Capacity (kWh) | 10.0 | Total battery capacity |
| Max charge power (kW) | 5.0 | Maximum charge rate |
| Max discharge power (kW) | 5.0 | Maximum discharge rate |
| Round-trip efficiency | 0.90 | Battery round-trip efficiency (0.5–1.0) |
| Min SoC (%) | 10.0 | Lower operating limit for optimization |
| Max SoC (%) | 90.0 | Upper operating limit for optimization |
| SoC sensor | — | State-of-charge sensor (% or kWh) |
| Power sensor (opt) | — | Real-time battery power sensor (W or kW) |
| DC PV efficiency (opt) | 0.97 | Efficiency of DC-coupled PV on this inverter's DC bus |

### PV Array Subentry

| Parameter | Default | Description |
|-----------|---------|-------------|
| Name (opt) | — | Display name for this array |
| Peak power (kWp) | 1.0 | Array peak output |
| Orientation (°) | 180 | Compass bearing: 0 = north, 90 = east, 180 = south, 270 = west |
| Tilt (°) | 35 | Panel tilt angle from horizontal |
| Efficiency factor (opt) | 0.85 | Derating for shading, soiling, inverter losses (AC-coupled) |
| DC-coupled | false | Enable if this array is on the battery inverter's DC bus |

---

## Entities Created

**Convention**: All power sensors *created by the integration* (e.g. Total Battery Power, Battery Setpoint) use **positive for discharge** and **negative for charge**.

The **Battery power sensor** you configure as input (`battery_power_sensor`) is the opposite: **positive for charging** and **negative for discharging**, matching how the integration reports charge/discharge mode. If your own battery sensor follows the output convention above, invert its sign before pointing this field at it.

### Sensors

**Optimization**

| Entity | Unit | Description |
|--------|------|-------------|
| Optimal Power | W | Battery power recommended by the DP optimizer for the current 15-min slot |
| Optimal Mode | — | Current mode: `charging`, `discharging`, `idle`, `zero_grid` |
| Schedule | — | Full schedule summary; detailed schedule available in attributes. Disabled by default to reduce recorder load. |

**Battery State**

| Entity | Unit | Description |
|--------|------|-------------|
| Total State of Charge | % | Combined SoC across all batteries (capacity-weighted average) |
| Total Battery Power | kW | Combined battery power across all batteries |
| Battery Setpoint | W | Combined real-time power target sent to all batteries (~5s updates) |
| Battery Setpoint [Name] | W | Per-battery power setpoint (split from the combined setpoint) |
| State of Charge [Name] | % | Per-battery state of charge |

**Financial**

| Entity | Unit | Description |
|--------|------|-------------|
| Shadow Price of Storage | EUR/kWh | Marginal value of 1 kWh stored right now, derived from the DP value function. Use as a charge/discharge decision threshold. |
| Estimated Savings | EUR | Cumulative financial benefit vs. doing nothing (running total) |

**Forecast**

| Entity | Unit | Description |
|--------|------|-------------|
| PV Forecast | kW | Current expected PV output; full hourly forecast in attributes |
| Consumption Forecast | kW | Current expected household consumption; full forecast in attributes |
| Net Grid Forecast | kW | Expected grid exchange without battery (consumption − PV); full forecast in attributes |
| PV Forecast [Name] | kW | Per-array PV forecast. Diagnostic, disabled by default. |

**Diagnostics** (disabled by default)

| Entity | Unit | Description |
|--------|------|-------------|
| Solar Irradiance | W/m² | Current GHI from open-meteo; logged to recorder for price model training |
| Wind Speed | m/s | Current wind speed from open-meteo; logged to recorder for price model training |
| Current Grid Power | kW | Measured grid exchange used by the zero-grid controller |
| Control Mode | — | Active control mode (diagnostic mirror of the select entity) |
| Optimization Status | — | Optimizer health: `ok`, `stale`, `failed`, `disabled`, or `initializing` |

### Binary Sensors

| Entity | Description |
|--------|-------------|
| PV Curtailment Suggested | ON when the feed-in price is negative — purely price-based: exporting costs money, so curtailing PV is worth considering |
| Use Maximum Power Suggested | ON when the grid buy price is negative — signals that consuming as much as possible (battery charge, flexible loads) is beneficial |

### Switch

| Entity | Description |
|--------|-------------|
| Optimization Enabled | Pause/resume the optimizer without changing any other settings. State is restored on HA restart. |

### Select

| Entity | Options | Description |
|--------|---------|-------------|
| Control Mode | `zero_grid`, `follow_schedule`, `hybrid`, `hybrid_plus`, `manual` | Active battery control strategy |

### Number Entities

| Entity | Range | Default | Description |
|--------|-------|---------|-------------|
| Degradation Cost | 0–0.20 EUR/kWh | 0.03 | Battery wear cost per kWh throughput; included in the optimizer's cost function |
| Minimum Price Spread | 0–0.50 EUR/kWh | 0.05 | Minimum buy/sell spread required before arbitrage is scheduled |
| Zero Grid Deadband | 0–500 W | 50 W | Grid power tolerance; setpoints are not updated within this band |
| Manual Power Setpoint | ±max power W | 0 W | Target power in `manual` mode (positive = discharge, negative = charge) |

---

## Control Modes

- **Zero Grid**: Minimize grid exchange in real-time using the battery.
- **Follow Schedule**: Execute the DP-optimized schedule exactly. When the commitment
  filter keeps an active charge/discharge locked within the same price period, that
  lock applies to the published controller setpoint too, not just the diagnostic
  `optimal_power` value.
- **Hybrid** (recommended): DP schedule for arbitrage, zero-grid for self-consumption.
  When the DP schedule says `idle` and no discharge is planned soon, Hybrid still
  opportunistically captures PV surplus into the battery via zero-grid — even if the
  feed-in price is currently positive and exporting that surplus would be more
  profitable. This trades a small amount of arbitrage profit for resilience (keeping
  headroom available for real-time consumption spikes, e.g. a cloud passing over the
  panels while a large appliance switches on). If you want surplus capture to follow
  the price forecast instead, use **Hybrid+**; for the strictly cost-optimal schedule
  with no opportunistic charging at all, use **Follow Schedule** — Hybrid is
  recommended for robustness, not maximum arbitrage profit.
- **Hybrid+**: Like Hybrid, but consults the price forecast before storing PV surplus.
  When the shadow price says the battery can be filled more cheaply later (e.g. a
  midday PV peak at low prices), the surplus is exported at the current feed-in price
  instead of charged, and the battery charges later from the cheaper surplus.
  When little future surplus is forecast, stored energy stays valuable (it displaces
  grid import or serves expensive evening hours), so the surplus is captured
  immediately — identical to Hybrid. Exporting only wins when the battery would fill
  up anyway or the stored energy has little future value.
- **Manual**: Target power set via `number.battery_controller_manual_power_setpoint`.

Change the active mode with the **Control Mode** select entity, or use a service call in an automation.

---

## Controlling Your Battery

Use an automation to read the controller power target and send commands to your inverter.
In `follow_schedule`, `optimal_power` is the optimizer recommendation and
`battery_setpoint` is the actual published controller target; under normal operation
they match, and if the commitment filter locks a charge/discharge step within the
same price period, the lock is reflected in `battery_setpoint` as well.

| Control Mode | Optimal Mode | Power Sensor to Use |
|-------------|-------------|---------------------|
| `follow_schedule` | `charging` / `discharging` | `sensor.battery_controller_battery_setpoint` (W) |
| `hybrid` / `hybrid_plus` / `zero_grid` | `charging` / `discharging` / `zero_grid` | `sensor.battery_controller_battery_setpoint` (W) |

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

## Removal

To remove the Battery Controller integration:

1. Go to **Settings → Devices & Services → Battery Controller**.
2. Click the three-dot menu and select **Delete**.
3. Confirm the deletion — this removes the integration, all subentries, and all associated entities.
4. Restart Home Assistant to ensure all entities are fully removed from the registry.

If entities remain after deletion, go to **Settings → Devices & Services → Entities**, filter by "battery_controller", and delete any remaining entries manually.

---

## Known Limitations

- **Optimization horizon**: The DP optimizer uses a rolling 24–36 hour horizon. Decisions near the end of the horizon depend on the shadow price (marginal value of stored energy) rather than explicit future prices. The shadow price converges over several days of operation.
- **Price forecast dependency**: The optimizer requires a price sensor with forecast attributes. Without future prices, only the historical model is used, which reduces arbitrage accuracy.
- **Day-ahead gap**: Day-ahead prices are typically published around 13:00 CET. Before that, the integration uses the self-learning historical price model. During this window the schedule may be less optimal.
- **Single aggregate battery**: Multiple batteries are aggregated into one virtual battery for optimization. Setpoints are then split proportionally. Batteries with very different chemistries or SoC ranges may not be split optimally.
- **LFP assumptions**: The battery efficiency model assumes flat efficiency across the SoC range (as is typical for LFP cells). Other chemistries with significant SoC-dependent efficiency variation are not modeled.
- **No direct hardware communication**: Battery Controller writes setpoints to HA sensor/number entities only. You are responsible for connecting these to your inverter via automations.
- **Consumption pattern learning**: The consumption model requires several weeks of kWh sensor history to build accurate patterns. During the initial period, forecasts may be less accurate.

---

## Troubleshooting

### No charge/discharge schedule is generated

- Check **Settings → System → Logs** for errors from `battery_controller`.
- Verify the **Optimization Status** sensor is `ok` (not `failed` or `initializing`).
- Confirm your price sensor has forecast attributes: **Developer Tools → States** → find your sensor → check attributes for `raw_today`, `raw_tomorrow`, or `forecast`.

### Battery is not charging or discharging as expected

- Check the **Control Mode** select entity — it must not be `manual` unless you intend that.
- Verify your automation is reading `sensor.battery_controller_battery_setpoint` (not `optimal_power`).
- In `follow_schedule` mode: the commitment filter may be holding an earlier setpoint within the same price period. Check the **Schedule** sensor attributes for the committed action.

### Optimizer always schedules idle / no arbitrage

- Ensure the price spread between cheap and expensive hours exceeds the **Minimum Price Spread** number entity (default 0.05 EUR/kWh) plus twice the **Degradation Cost** (default 0.03 EUR/kWh).
- Check that the feed-in price is not equal to the grid price — if they are the same, arbitrage is less profitable. Configure a separate feed-in price sensor or set the fixed feed-in price.

### Entities are unavailable

- The integration marks entities unavailable when a coordinator fails to update. Check the logs for HTTP errors from open-meteo.com.
- If the SoC sensor is unavailable, the optimizer falls back to the last known SoC. Check that the SoC sensor entity is working correctly.

### PV forecast is always zero

- Verify your PV array subentry has the correct peak power, orientation, and tilt configured.
- Check that open-meteo.com is reachable from your Home Assistant instance.

### Diagnostics and analyzer

Download diagnostics via **Settings → Devices & Services → Battery Controller → three-dot menu → Download diagnostics** and upload to the [online analyzer](https://bvweerd.github.io/battery_controller/) for a full breakdown of your configuration, schedule, and optimizer decisions.

---

## License

See [LICENSE](LICENSE) for details.

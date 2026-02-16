# Battery Controller

**Home battery cost optimization for Home Assistant**

[![GitHub Release](https://img.shields.io/github/release/bvweerd/battery_controller.svg?style=flat-square)](https://github.com/bvweerd/battery_controller/releases)
[![License](https://img.shields.io/github/license/bvweerd/battery_controller.svg?style=flat-square)](LICENSE)
[![hacs](https://img.shields.io/badge/HACS-Default-orange.svg?style=flat-square)](https://hacs.xyz)

---

## High-level Description

This Home Assistant custom integration optimizes your home battery to minimize electricity costs. It uses dynamic programming to calculate the optimal charge/discharge schedule based on electricity price forecasts, PV production forecasts, and your household's energy consumption patterns. By intelligently deciding when to charge from the grid or solar, and when to discharge to power your home or sell back to the grid, it helps you save money and maximize your investment in a home battery and solar panels.

## Installation

### Prerequisites
- A dynamic electricity price sensor with forecast attributes (e.g., Nordpool, ENTSO-E, or [Dynamic Energy Contract Calculator](https://github.com/bvweerd/dynamic_energy_contract_calculator))
- A battery SoC sensor from your inverter integration.
- [HACS](https://hacs.xyz/) installed in your Home Assistant.

### Installation via HACS (Recommended)
1.  Navigate to the HACS section in your Home Assistant.
2.  Go to "Integrations", and click the three dots in the top right corner and select "Custom repositories".
3.  Enter `https://github.com/bvweerd/battery_controller` in the "Repository" field, select "Integration" as the category, and click "Add".
4.  The "Battery Controller" integration will now be shown. Click "Install" and proceed with the installation.
5.  Restart Home Assistant.

### Manual Installation
1.  Copy the `custom_components/battery_controller` directory to your Home Assistant's `custom_components` directory.
2.  Restart Home Assistant.

After installation, the integration can be added and configured through the UI:
**Settings -> Devices & Services -> Add Integration -> Battery Controller**

## Removal

1.  Go to **Settings -> Devices & Services**.
2.  Find the "Battery Controller" integration and click the three dots.
3.  Select "Delete" and confirm.
4.  To completely remove it, use HACS to uninstall the repository or manually delete the `battery_controller` folder from your `custom_components` directory.
5.  Restart Home Assistant.

## Supported Devices

This is a calculated integration and does not directly communicate with any specific hardware. It works with any battery inverter and electricity meter as long as they provide the required sensors in Home Assistant.

## Known Limitations

- The optimization is only as good as the forecasts it receives. Inaccurate price, PV, or consumption forecasts will lead to a suboptimal schedule.
- The household consumption forecast is based on historical data and does not account for future one-off events (e.g., having a party, going on vacation).

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
- Terminal condition: `V[T][s] = -(stored_kwh × feed_in_price_T)` — stored energy at the end of the horizon retains value at the last known feed-in price. This prevents the optimizer from irrationally discharging the battery just before the horizon ends.

**4. Shadow Price of Stored Energy**

After the backward pass, the optimizer computes the **shadow price** (marginal value) of stored energy at the current SoC:

```
λ = -dV[0]/dSoC ≈ (V[0][s-1] - V[0][s+1]) / (2 × ΔSoC)
```

This answers: *"If I have 1 extra kWh in the battery right now, how much will my future electricity costs decrease?"*

The shadow price is used as an economic threshold for real-time decisions in hybrid mode:
- **Export/discharge**: worth it when `feed_in × √RTE ≥ λ` (selling captures at least as much value as keeping it)
- **Charge from grid**: worth it when `buy_price ≤ λ / √RTE` (buying is cheaper than the future saving it will generate)

The **Shadow Price of Storage** sensor exposes this value and its derived thresholds for use in external automations.

**5. Algorithm Steps**

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

**6. Oscillation Prevention**

After the DP optimizer produces the initial schedule, a post-processing filter removes unprofitable charge/discharge oscillations:

```python
# Minimum profitable price spread for arbitrage
min_spread_required = 2 * degradation / sqrt(RTE) + min_price_spread

# For RTE=0.83, degradation=€0.03, min_price_spread=€0.05:
# => min_spread_required ≈ €0.116/kWh

# Check each charge/discharge pair within 2-hour window
if P_discharge - P_charge / RTE < min_spread_required:
    replace_with_idle()  # Not profitable enough
```

This prevents the battery from oscillating (charge → discharge → charge) when price differences are too small to justify the round-trip losses. The **Min Price Spread** number entity lets you adjust this threshold at runtime.

**7. Key Decisions**

The optimizer automatically handles:
- **Price arbitrage**: Charge during cheap hours (€0.05), discharge during expensive hours (€0.30)
- **Feed-in optimization**:
  - High feed-in (€0.10) + low future prices → Export now (don't store)
  - Low feed-in (€0.04) + high future prices (€0.30) → Store for later
- **Negative buy price** (e.g. Tibber during wind surplus): Import and charge at maximum rate — you are paid to consume
- **Negative feed-in price** (e.g. solar overproduction): Charge battery to avoid paying for exports; switches to `follow_schedule` mode so curtailing PV does not create a zero-grid deadlock
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
| Battery SoC sensor | Battery state-of-charge sensor (% or kWh). If temporarily unavailable, the last known SoC from a previous optimization run will be used as a fallback. |

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
| **Power consumption sensors** | One or more DSMR **power** sensors (W) for real-time grid balance |
| **Power production sensors** | One or more DSMR **power** sensors (W) for real-time grid balance |
| Electricity consumption sensors | One or more DSMR cumulative kWh sensors for consumption (e.g. tariff 1 + tariff 2) |
| Electricity production sensors | One or more DSMR cumulative kWh sensors for production |

**Power sensors (W)** are used for real-time zero_grid control (~5s updates). Grid balance = sum(consumption) - sum(production). These are typically the instantaneous power sensors from DSMR (e.g. `sensor.power_consumption`, `sensor.power_production`).

**Energy sensors (kWh)** are used to learn your household's consumption pattern from historical data. These are typically the cumulative kWh sensors created by a DSMR smart meter integration (e.g. `sensor.electricity_consumed_tariff_1`, `sensor.electricity_consumed_tariff_2`).

### Advanced (collapsed)

| Parameter | Default | Description |
|-----------|---------|-------------|
| Time step (minutes) | 15 | Optimization resolution |
| Optimization interval (minutes) | 15 | How often to re-optimize |
| Fixed feed-in price (EUR/kWh) | 0.07 | Fallback feed-in price when no sensor configured |
| Zero-grid enabled | true | Enable zero-grid control mode |

## Entities Created

### Sensors (13)

| Entity | Unit | Description | Attributes |
|--------|------|-------------|------------|
| **Optimal Power** | kW | **Strategy**: Battery power from DP optimizer (15-min planning). Use in `follow_schedule` mode. | `optimal_mode`, `current_price` |
| Optimal Mode | — | Current mode: `charging`, `discharging`, `idle`, `zero_grid`, `manual` | — |
| **Schedule** | — | Full optimization schedule (see below) | `power_schedule_kw`, `mode_schedule`, `soc_schedule_kwh`, `price_forecast` |
| State of Charge | % | Current battery SoC | `soc_kwh`, `power_kw`, `mode` |
| Battery Power | kW | Current battery power | — |
| PV Forecast | kW | Current PV production | `forecast_kw`, `dc_forecast_kw`\*, `current_dc_pv_kw`\* |
| Consumption Forecast | kW | Current consumption estimate | `forecast_kw` |
| Net Grid Forecast | kW | Net grid power (positive=import) | `forecast_kw` |
| Estimated Savings | EUR | **Net financial impact of battery actions** (sum of direct profits/losses per step, including degradation and PV opportunity cost) over the planning horizon. | `baseline_cost`, `optimized_cost`, `step_profit_loss_eur` |
| **Shadow Price of Storage** | EUR/kWh | **Marginal value of 1 kWh stored right now**, derived from the DP value function. Use as a threshold for external automations. | `discharge_threshold_eur_kwh`, `charge_threshold_eur_kwh` |
| **Grid Setpoint** | W | **Tactics**: Real-time battery power from zero-grid controller (~5s updates). Use in `hybrid`/`zero_grid` modes. | `target_power_w`, `current_grid_w`, `current_battery_w`, `dp_schedule_w`, `mode`, `action_mode`, `soc_kwh`, `soc_percent` |
| Control Mode | — | Current control mode | — |
| Optimization Status | — | Optimizer health (`ok`/`error`/`waiting`) | `n_steps`, `total_cost`, `baseline_cost`, `savings`, `current_price`, `timestamp` |

\* Only present when DC-coupled PV is configured.

All `forecast_kw` attributes are lists at **optimizer step resolution** (default: 15 minutes). Step 0 = current, step 1 = next step, etc. The PV, Consumption, and Net Grid sensors each have their own `forecast_kw`, all at the same resolution and time alignment as the Schedule sensor.

#### Optimal Power vs Grid Setpoint

These two sensors serve **different purposes** and update at different frequencies:

| Aspect | Optimal Power | Grid Setpoint |
|--------|---------------|---------------|
| **Source** | DP optimizer (strategic planning) | Zero-grid controller (tactical execution) |
| **Update frequency** | Every 15 minutes | Every ~5 seconds (when grid sensor configured) |
| **Purpose** | Long-term cost optimization | Real-time grid exchange minimization |
| **Value in follow_schedule** | DP schedule power (e.g., +0.5 kW) | Same as Optimal Power |
| **Value in hybrid** | DP schedule or 0 (idle) | Real-time calculated (e.g., -680W to zero grid) |
| **Value in zero_grid** | Always 0 kW | Real-time calculated |
| **Use in automation** | ✅ follow_schedule mode | ✅ hybrid/zero_grid modes |

**Example (hybrid mode):**
- Situation: Grid importing 680W, battery charging 258W, PV producing 348W
- **Optimal Power**: 0 kW (DP says "idle", let zero-grid decide)
- **Grid Setpoint**: -680W (real-time: "discharge to eliminate grid import")

**Which to use?**
- **follow_schedule**: Use `optimal_power` (follows DP schedule exactly)
- **hybrid/zero_grid**: Use `battery_setpoint` (real-time, more accurate)
- **Monitoring**: Use both to see strategy vs execution

### Schedule Sensor (detail)

The **Schedule** sensor is the core output of the optimizer. Its state shows a summary like `C:4 D:6 I:14` (4 charging, 6 discharging, 14 idle steps). All data is in the attributes:

| Attribute | Type | Description |
|-----------|------|-------------|
| `power_schedule_kw` | `list[float]` | Battery power for each time step. Positive = charge, negative = discharge. Length equals the number of planning steps (e.g. 96 for 24h at 15-min resolution). |
| `mode_schedule` | `list[str]` | Mode per step: `"charging"`, `"discharging"`, or `"idle"`. Same length as `power_schedule_kw`. |
| `soc_schedule_kwh` | `list[float]` | Predicted state-of-charge (kWh) at the start of each step. |
| `price_forecast` | `list[float]` | Electricity buy price (EUR/kWh) used for each step. |
| `step_profit_loss_eur` | `list[float]` | Financial profit or loss (EUR) for each time step, attributable to direct battery actions. |

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

| Entity | Range | Default | Description |
|--------|-------|---------|-------------|
| Minimum SoC | 0-50% | 12% | Runtime adjustable min SoC |
| Maximum SoC | 50-100% | 100% | Runtime adjustable max SoC |
| Degradation Cost | 0-0.20 EUR/kWh | 0.03 | Battery wear cost per kWh throughput (accounts for battery replacement cost) |
| **Min Price Spread** | 0-0.50 EUR/kWh | **0.05** | **Minimum price spread to trigger arbitrage**. Prevents oscillation when price differences are too small. Increase if battery cycles too frequently on small price variations. |
| Zero Grid Deadband | 0-500 W | 10 W | Deadband for zero-grid mode (prevents rapid switching) |

### Select Entity

| Entity | Options | Description |
|--------|---------|-------------|
| Control Mode | zero_grid, follow_schedule, hybrid, manual | Battery control strategy |

### Switch Entity

| Entity | Description |
|--------|-------------|
| Optimization Enabled | Enable/disable the optimizer |

### Binary Sensors (2)

| Entity | Device class | Description |
|--------|-------------|-------------|
| **PV Curtailment Suggested** | problem | ON when the feed-in price is negative **and** the battery can no longer absorb the surplus (SoC near maximum, or actual charging power significantly below the setpoint). Suggests reducing PV inverter output. |
| **Use Maximum Power Suggested** | running | ON when the grid buy price is negative (you are paid to consume). Suggests running flexible loads and charging the battery at full rate. |

> **Tip**: Both sensors expose price and battery data as attributes for use in automations.

### Shadow Price of Storage (detail)

The **Shadow Price of Storage** sensor (`sensor.battery_controller_shadow_price`) exposes the marginal value of 1 kWh stored in the battery right now (EUR/kWh), derived from the DP value function after every optimization run.

**Attributes:**

| Attribute | Description |
|-----------|-------------|
| `shadow_price_eur_kwh` | Same as state value |
| `discharge_threshold_eur_kwh` | `shadow_price × √RTE` — minimum feed-in price at which exporting is worth at least as much as keeping the energy |
| `charge_threshold_eur_kwh` | `shadow_price / √RTE` — maximum buy price at which charging from the grid is still economically justified |

**Example use cases:**

```yaml
# External automation: charge an EV or other flexible load
# when the current buy price is below the charge threshold
- condition: template
  value_template: >
    {{ states('sensor.current_electricity_price') | float <
       state_attr('sensor.battery_controller_shadow_price', 'charge_threshold_eur_kwh') | float }}

# External automation: trigger export / peak shaving
# when the feed-in price exceeds the discharge threshold
- condition: template
  value_template: >
    {{ states('sensor.current_feed_in_price') | float >
       state_attr('sensor.battery_controller_shadow_price', 'discharge_threshold_eur_kwh') | float }}
```

The shadow price naturally reflects all future price information available to the optimizer. When expensive hours are approaching, the shadow price is high (→ don't sell cheaply now). When prices are flat, the shadow price is close to the feed-in price (→ exporting is fine).

## Control Modes

- **Zero Grid**: Minimize grid exchange in real-time using the battery
- **Follow Schedule**: Execute the DP-optimized schedule exactly
- **Hybrid** (recommended): DP schedule for price arbitrage (charge/discharge), zero-grid for self-consumption during idle periods
- **Manual**: No automatic control

## Controlling Your Battery

Battery Controller calculates the optimal schedule but does **not** directly control your inverter. You need an automation that reads the `Optimal Mode` and `Optimal Power` sensors and sends the corresponding commands to your inverter integration.

### Key Sensors for Control

| Sensor | Values | Update Frequency | Use When |
|--------|--------|------------------|----------|
| `sensor.battery_controller_optimal_mode` | `charging`, `discharging`, `idle`, `zero_grid`, `manual` | Every 15 min | Always (indicates what mode) |
| `sensor.battery_controller_optimal_power` | float (kW) | Every 15 min | **follow_schedule** mode |
| `sensor.battery_controller_battery_setpoint` | float (W) | Every ~5 sec | **hybrid** / **zero_grid** modes |

**Which power sensor to use?**
- **follow_schedule**: Use `optimal_power` (DP schedule, strategic planning)
- **hybrid / zero_grid**: Use `battery_setpoint` (real-time calculated, tactical execution)

The `optimal_mode` reflects the active **control mode**:

| Control Mode | Optimal Mode | Power Sensor to Use | Behavior |
|-------------|-------------|---------------------|----------|
| `follow_schedule` | `charging` / `discharging` / `idle` | `optimal_power` (kW) | Execute the optimized schedule exactly |
| `hybrid` | `charging` / `discharging` / `zero_grid` | `battery_setpoint` (W) | Arbitrage from schedule + real-time self-consumption |
| `zero_grid` | `zero_grid` | `battery_setpoint` (W) | Real-time grid minimization |
| `manual` | `manual` | (neither) | No automatic control |

The raw DP schedule is always available via the **Schedule** sensor attributes, regardless of control mode.

### Zero-Grid Modes

The integration supports two zero-grid modes:

#### HA-Controlled Zero-Grid (with DSMR power sensors)
- Configure **Power consumption sensors** and **Power production sensors** in the integration
- Grid balance is calculated: sum(consumption) - sum(production)
- `battery_setpoint` is updated every ~5s with real-time calculated setpoint
- Home Assistant controls the battery based on actual grid measurements
- Most accurate for systems without built-in zero-grid capability

#### Battery-Controlled Zero-Grid (without power sensors)
- Don't configure power sensors (or leave them empty)
- `optimal_mode` = "zero_grid" when optimizer wants zero-grid behavior
- `battery_setpoint` = 0 (battery inverter handles zero-grid with its own sensors)
- Your automation sets the battery to its built-in zero-grid mode
- Best for inverters with good built-in zero-grid/self-consumption modes

### Zero-Grid with Automation

If you want to implement zero-grid yourself via an automation, use `sensor.battery_controller_battery_setpoint`.

**With power sensors** (HA-controlled), the sensor calculates:

```
target = battery_w - grid_w  (= PV - load = net house demand)
```

This is a direct calculation, not a feedback loop. Example:

| battery_w | grid_w | target |
|-----------|--------|--------|
| 0 | +300 | −300 W (start discharging) |
| −300 | 0 | −300 W (keep discharging) |
| −300 | +100 | −400 W (load increased, discharge more) |
| −400 | 0 | −400 W (stable again) |

The **Zero Grid Deadband** (default 10 W) suppresses small fluctuations: if the new target is within the deadband of the previous one, the sensor state does not change and the automation does not fire — the inverter keeps its last setpoint automatically.

**Example: HA-controlled zero-grid** (with power sensors configured):

```yaml
automation:
  - alias: "Battery Controller - HA-controlled Zero grid"
    description: "Mirror battery_setpoint sensor to inverter battery power"
    trigger:
      - platform: state
        entity_id: sensor.battery_controller_battery_setpoint
    action:
      - variables:
          power_w: "{{ states('sensor.battery_controller_battery_setpoint') | float(0) }}"

      - choose:
          # Charging
          - conditions:
              - condition: template
                value_template: "{{ power_w > 0 }}"
            sequence:
              - service: number.set_value
                target:
                  entity_id: number.YOUR_INVERTER_charge_power
                data:
                  value: "{{ power_w | round(0) }}"
              - service: select.select_option
                target:
                  entity_id: select.YOUR_INVERTER_mode
                data:
                  option: "Force Charge"

          # Discharging
          - conditions:
              - condition: template
                value_template: "{{ power_w < 0 }}"
            sequence:
              - service: number.set_value
                target:
                  entity_id: number.YOUR_INVERTER_discharge_power
                data:
                  value: "{{ power_w | abs | round(0) }}"
              - service: select.select_option
                target:
                  entity_id: select.YOUR_INVERTER_mode
                data:
                  option: "Force Discharge"

          # Exactly 0: SoC limit hit or mode is not zero_grid
        default:
          - service: select.select_option
            target:
              entity_id: select.YOUR_INVERTER_mode
            data:
              option: "Auto"
```

> **Note**: Set `select.battery_controller_control_mode` to `zero_grid` so the `battery_setpoint` sensor continuously calculates the target.

**Example: Battery-controlled zero-grid** (without power sensors):

```yaml
automation:
  - alias: "Battery Controller - Battery-controlled Zero grid"
    description: "Set battery mode based on optimal_mode"
    trigger:
      - platform: state
        entity_id: sensor.battery_controller_optimal_mode
    action:
      - service: select.select_option
        target:
          entity_id: select.YOUR_INVERTER_mode
        data:
          option: >
            {% set mode = states('sensor.battery_controller_optimal_mode') %}
            {% if mode == 'zero_grid' %}
              Self Consumption
            {% elif mode == 'charging' %}
              Force Charge
            {% elif mode == 'discharging' %}
              Force Discharge
            {% else %}
              Auto
            {% endif %}
```

> **Note**: Replace "Self Consumption" with your inverter's zero-grid mode name (e.g., "Maximize Self Consumption", "Zero Export", "Load First").

### Example Automation

This automation uses the **correct sensor** for each control mode. Adapt the entity IDs and service calls to your specific inverter integration.

```yaml
automation:
  - alias: "Battery Controller - Control inverter"
    description: "Set inverter mode and power based on Battery Controller"
    trigger:
      - platform: state
        entity_id: sensor.battery_controller_optimal_mode
      - platform: state
        entity_id: sensor.battery_controller_optimal_power
      - platform: state
        entity_id: sensor.battery_controller_battery_setpoint
    action:
      - variables:
          # Use battery_setpoint for hybrid/zero_grid (real-time), optimal_power for follow_schedule
          control_mode: "{{ states('select.battery_controller_control_mode') }}"
          optimal_mode: "{{ states('sensor.battery_controller_optimal_mode') }}"

          # Select the right power sensor based on control mode
          power_w: >
            {% if control_mode in ['hybrid', 'zero_grid'] %}
              {{ states('sensor.battery_controller_battery_setpoint') | float }}
            {% else %}
              {{ (states('sensor.battery_controller_optimal_power') | float * 1000) | round(0) }}
            {% endif %}

      - choose:
          # Charging
          - conditions:
              - condition: template
                value_template: "{{ optimal_mode == 'charging' or power_w > 50 }}"
            sequence:
              - service: number.set_value
                target:
                  entity_id: number.YOUR_INVERTER_charge_power
                data:
                  value: "{{ power_w | abs | round(0) }}"
              - service: select.select_option
                target:
                  entity_id: select.YOUR_INVERTER_mode
                data:
                  option: "Force Charge"

          # Discharging
          - conditions:
              - condition: template
                value_template: "{{ optimal_mode == 'discharging' or power_w < -50 }}"
            sequence:
              - service: number.set_value
                target:
                  entity_id: number.YOUR_INVERTER_discharge_power
                data:
                  value: "{{ power_w | abs | round(0) }}"
              - service: select.select_option
                target:
                  entity_id: select.YOUR_INVERTER_mode
                data:
                  option: "Force Discharge"

          # Idle or Manual: set inverter to auto/standby
        default:
          - service: select.select_option
            target:
              entity_id: select.YOUR_INVERTER_mode
            data:
              option: "Auto"
```

**Key improvements:**
- ✅ Uses `battery_setpoint` (W) for `hybrid` and `zero_grid` modes (real-time, accurate)
- ✅ Uses `optimal_power` (kW) for `follow_schedule` mode (follows DP schedule)
- ✅ Triggers on both sensors to catch all updates
- ✅ Single logic for charge/discharge based on power value

### Alternative: Manual Mode + Power Target

Some inverters (e.g., Growatt, Solis) control charge/discharge via:
- **Manual mode** + target power in a number entity (for force charge/discharge)
- **Built-in zero-grid mode** that handles self-consumption automatically

This automation is simpler because zero-grid just switches the inverter mode without needing power control:

```yaml
automation:
  - alias: "Battery Controller - Manual mode control"
    description: "Control battery via manual mode + power target"
    trigger:
      - platform: state
        entity_id: sensor.battery_controller_optimal_mode
      - platform: state
        entity_id: sensor.battery_controller_battery_setpoint
    action:
      - variables:
          control_mode: "{{ states('select.battery_controller_control_mode') }}"
          optimal_mode: "{{ states('sensor.battery_controller_optimal_mode') }}"

          # Use battery_setpoint for hybrid/zero_grid, optimal_power for follow_schedule
          power_w: >
            {% if control_mode in ['hybrid', 'zero_grid'] %}
              {{ states('sensor.battery_controller_battery_setpoint') | float }}
            {% else %}
              {{ (states('sensor.battery_controller_optimal_power') | float * 1000) | round(0) }}
            {% endif %}

      - choose:
          # Charging (power > 50W)
          - conditions:
              - condition: template
                value_template: "{{ power_w > 50 }}"
            sequence:
              - service: select.select_option
                target:
                  entity_id: select.YOUR_INVERTER_working_mode
                data:
                  option: "Manual"
              - service: number.set_value
                target:
                  entity_id: number.YOUR_INVERTER_target_power
                data:
                  value: "{{ power_w | abs | round(0) }}"

          # Discharging (power < -50W)
          - conditions:
              - condition: template
                value_template: "{{ power_w < -50 }}"
            sequence:
              - service: select.select_option
                target:
                  entity_id: select.YOUR_INVERTER_working_mode
                data:
                  option: "Manual"
              - service: number.set_value
                target:
                  entity_id: number.YOUR_INVERTER_target_power
                data:
                  value: "{{ power_w | round(0) }}"  # Keep negative for discharge

          # Idle / Zero-grid: Use inverter's built-in self-consumption mode
        default:
          - service: select.select_option
            target:
              entity_id: select.YOUR_INVERTER_working_mode
            data:
              option: "Self Use"  # or "Maximize Self Consumption", "Zero Export"
```

**How this differs:**
- ✅ Single `target_power` entity for both charge (positive) and discharge (negative)
- ✅ Inverter mode "Manual" for force charge/discharge
- ✅ Inverter mode "Self Use" handles zero-grid automatically (no power values needed)
- ✅ Simpler: no separate charge_power and discharge_power entities

**Common inverter modes:**
- Manual mode: `Manual`, `Passive`, `Battery First`
- Zero-grid mode: `Self Use`, `Maximize Self Consumption`, `Zero Export`, `Load First`

### Inverter Integration

Replace `YOUR_INVERTER` with your actual inverter entity names. Most battery inverters expose:
- A **mode select entity** to control operation mode (charge/discharge/auto)
- A **power number entity** to set charge/discharge power limits

Check your inverter's Home Assistant integration documentation for the correct entity names and available modes.

**Common entity patterns**:
- Mode: `select.{inverter}_mode`, `select.{inverter}_operation_mode`, `select.{inverter}_working_mode`
- Power: `number.{inverter}_charge_power`, `number.{inverter}_discharge_power`, `number.{inverter}_max_power`

> **Tip**: The automation automatically uses the right sensor (`battery_setpoint` for hybrid/zero_grid, `optimal_power` for follow_schedule). No manual switching needed!

> **Tip**: Set `select.battery_controller_control_mode` to `manual` to temporarily disable the automation and take manual control of your inverter.

> **Note**: If your inverter requires separate entities for charge and discharge power, you may need to split the charge/discharge sequences to set the appropriate entity.

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

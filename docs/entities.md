# Entity reference

!!! abstract "Sign convention"
    All power sensors **created by** the integration (Total Battery Power, Battery
    Setpoint, Optimal Power) use **positive for discharge** and **negative for charge**.

    The battery power sensor you **configure as input** (`battery_power_sensor`) is the
    opposite: positive for charging, negative for discharging. See
    [sign conventions](inverter-control.md#sign-conventions).

Entities marked *disabled by default* exist but are not enabled until you turn them on in
the entity registry. They are disabled to keep recorder load down.

---

## Sensors

### Optimization

| Entity | Unit | Description |
|--------|------|-------------|
| Optimal Power | W | Battery power recommended by the DP optimizer for the current 15-minute slot. Diagnostic — do not drive your inverter from this. |
| Optimal Mode | — | Current mode: `charging`, `discharging`, `idle`, `zero_grid` |
| Schedule | — | Full schedule summary; the detailed schedule is in the attributes. *Disabled by default.* |

### Battery state

| Entity | Unit | Description |
|--------|------|-------------|
| Total State of Charge | % | Combined SoC across all batteries (capacity-weighted average) |
| Total Battery Power | kW | Combined battery power across all batteries |
| Battery Setpoint | W | Combined real-time power target sent to all batteries (~5 s updates). **This is the one to read in automations.** |
| Battery Setpoint *[Name]* | W | Per-battery power setpoint, split from the combined setpoint |
| State of Charge *[Name]* | % | Per-battery state of charge |

### Financial

| Entity | Unit | Description |
|--------|------|-------------|
| Shadow Price of Storage | EUR/kWh | Marginal value of 1 kWh stored right now, derived from the DP value function. Usable as a charge/discharge decision threshold in your own automations. |
| Estimated Savings | EUR | Cumulative financial benefit versus doing nothing (running total) |

### Forecast

| Entity | Unit | Description |
|--------|------|-------------|
| PV Forecast | kW | Current expected PV output; the full hourly forecast is in the attributes |
| Consumption Forecast | kW | Current expected household consumption; full forecast in the attributes |
| Net Grid Forecast | kW | Expected grid exchange without battery (consumption − PV); full forecast in the attributes |
| PV Forecast *[Name]* | kW | Per-array PV forecast. *Disabled by default.* |

### Diagnostics

All *disabled by default*.

| Entity | Unit | Description |
|--------|------|-------------|
| Solar Irradiance | W/m² | Current GHI from open-meteo; logged to the recorder to train the price model |
| Wind Speed | m/s | Current wind speed from open-meteo; logged to the recorder to train the price model |
| Current Grid Power | kW | Measured grid exchange used by the zero-grid controller |
| Control Mode | — | Active control mode (diagnostic mirror of the select entity) |
| Optimization Status | — | Optimizer health: `ok`, `stale`, `failed`, `disabled`, or `initializing` |

!!! tip
    **Solar Irradiance** and **Wind Speed** feed the self-learning historical price model.
    Enabling them early gives the model recorder history to learn from sooner.

---

## Binary sensors

| Entity | Description |
|--------|-------------|
| PV Curtailment Suggested | ON when the feed-in price is negative — purely price-based: exporting costs money, so curtailing PV is worth considering |
| Use Maximum Power Suggested | ON when the grid buy price is negative — consuming as much as possible (battery charge, flexible loads) is beneficial |

Both are suggestions, not actions. Wire them to an automation if you want them acted on.

---

## Switch

| Entity | Description |
|--------|-------------|
| Optimization Enabled | Pause or resume the optimizer without changing any other setting. State is restored across Home Assistant restarts. |

---

## Select

| Entity | Options | Description |
|--------|---------|-------------|
| Control Mode | `zero_grid`, `follow_schedule`, `hybrid`, `hybrid_plus`, `manual` | Active battery control strategy — see [control modes](control-modes.md) |

---

## Number entities

| Entity | Range | Default | Description |
|--------|-------|---------|-------------|
| Degradation Cost | 0–1.00 EUR/cycle | 0.04 | Battery wear cost per full charge+discharge cycle; part of the optimizer's cost function |
| Minimum Price Spread | 0–0.50 EUR/kWh | 0.05 | Minimum buy/sell spread required before arbitrage is scheduled |
| Zero Grid Deadband | 0–500 W | 50 | Grid power tolerance; setpoints are not updated within this band |
| Manual Power Setpoint | ±max power W | 0 | Target power in `manual` mode (positive = discharge, negative = charge) |

See [runtime tuning](configuration.md#runtime-tuning-number-entities) for how degradation
cost and minimum price spread interact to set the profitability bar.

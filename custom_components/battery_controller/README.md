# Battery Controller

Home Assistant custom integration for battery cost optimization using dynamic programming.

## Sensors

### Required Sensors

| Sensor | Description |
|--------|-------------|
| **Electricity price sensor** | Dynamic electricity buy price (e.g. Nordpool, ENTSO-E). Used by the optimizer to determine optimal charge/discharge timing. |
| **Battery SoC sensor** | Current battery state of charge (%). Required for the optimizer to know the starting point of each optimization cycle. |

### Optional Sensors

| Sensor | Effect when configured | Effect when missing |
|--------|----------------------|---------------------|
| **Feed-in price sensor** | Optimizer uses dynamic feed-in tariffs for discharge decisions. | Falls back to the fixed feed-in price setting (default 0.07 EUR/kWh). Optimization still works but assumes a constant sell price. |
| **Battery power sensor** | Real-time battery charge/discharge power reading. Enables accurate battery mode detection (charging/discharging/idle). | Battery mode defaults to "idle". Mode detection becomes unreliable. |
| **Consumption sensor** | Real-time house consumption. Used to build hourly consumption patterns (14-day lookback) for better forecasts. Improves zero-grid and hybrid mode accuracy. | Falls back to generic time-of-day/day-of-week consumption patterns. Optimization works but with less accurate consumption estimates. |
| **Grid power sensor** | Real-time grid import/export measurement. **Required for zero-grid mode** — the controller needs this to drive grid exchange to zero. Also important for hybrid mode (real-time grid error correction). | Defaults to 0 W (assumes grid is balanced). **Zero-grid mode will not function** — returns 0 W setpoint. Hybrid mode loses real-time correction but still follows the DP schedule. Follow-schedule mode is unaffected. |

### Sensor Requirements per Control Mode

| Control Mode | grid_power_sensor | battery_power_sensor | consumption_sensor |
|---|---|---|---|
| **Zero-grid** | **Required** | Recommended | Recommended |
| **Hybrid** | Important | Recommended | Recommended |
| **Follow schedule** | Not used | Optional | Optional |
| **Manual** | Not used | Not used | Not used |

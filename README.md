# Battery Controller

**Home battery cost optimization for Home Assistant**

[![GitHub Release](https://img.shields.io/github/release/bvweerd/battery_controller.svg?style=flat-square)](https://github.com/bvweerd/battery_controller/releases)
[![License](https://img.shields.io/github/license/bvweerd/battery_controller.svg?style=flat-square)](LICENSE)
[![hacs](https://img.shields.io/badge/HACS-Default-orange.svg?style=flat-square)](https://hacs.xyz)

---

> **⚠️ This integration is aimed at technically experienced users.**
> It requires setting up and tuning a number of parameters (efficiency, degradation costs, price sensors, control mode) and connecting it to your inverter via automations. Incorrect configuration will not damage your battery, but may result in suboptimal scheduling or no control at all. If you are not comfortable reading diagnostics data and interpreting optimizer output, this integration may not be the right fit — yet.

---

## 📖 Documentation

**[▶ bvweerd.github.io/battery_controller](https://bvweerd.github.io/battery_controller/)**

Installation, the full configuration reference, control modes, inverter wiring, the
algorithm, an FAQ built from real issue reports, and troubleshooting.

| | |
|---|---|
| [Installation](https://bvweerd.github.io/battery_controller/installation/) | Prerequisites, verifying your price and consumption sensors, HACS setup |
| [Configuration reference](https://bvweerd.github.io/battery_controller/configuration/) | Every option in the main entry, battery subentries and PV arrays |
| [Control modes](https://bvweerd.github.io/battery_controller/control-modes/) | Zero-grid, follow-schedule, hybrid, hybrid+, manual |
| [Connecting your inverter](https://bvweerd.github.io/battery_controller/inverter-control/) | Which sensor to read, sign conventions, example automation |
| [How it works](https://bvweerd.github.io/battery_controller/how-it-works/) | Coordinator cascade, price model, why the schedule changes |
| [Algorithm](https://bvweerd.github.io/battery_controller/algorithm/) | The DP engine, step by step |
| [Efficiency curves](https://bvweerd.github.io/battery_controller/efficiency-curves/) | Measured curves for named home battery systems |
| [FAQ](https://bvweerd.github.io/battery_controller/faq/) | Modes, sensors, signs, forecasts |
| [Troubleshooting](https://bvweerd.github.io/battery_controller/troubleshooting/) | Symptom-driven fixes |

---

## 🔍 Diagnostics Analyzer

**Upload your `diagnostics.json` to the online analyzer for a full breakdown of your configuration, optimizer schedule, profitability analysis, and improvement tips — no installation required.**

**[▶ Open Battery Controller Analyzer](https://bvweerd.github.io/battery_controller/analyzer/)**

[![Battery Controller Analyzer screenshot](docs/assets/analyzer.png)](https://bvweerd.github.io/battery_controller/analyzer/)

The analyzer runs entirely in your browser. It visualizes the current schedule, re-runs the DP optimizer with your data, and explains every charge/discharge decision. To generate a diagnostics file: **Settings → Devices & Services → Battery Controller → three-dot menu → Download diagnostics**.

---

## What is Battery Controller?

This Home Assistant custom integration optimizes your home battery to minimize electricity costs. It uses **dynamic programming** (backward induction) to calculate the optimal charge/discharge schedule based on:

- **Electricity price forecasts** (Nordpool, ENTSO-E, OMIE, or any price sensor with forecast attributes like the [Dynamic Energy Contract Calculator](https://github.com/bvweerd/dynamic_energy_contract_calculator))
- **PV production forecasts** (from open-meteo.com solar radiation data, or from a PV forecast integration such as [Solcast](https://github.com/BJReplay/ha-solcast-solar))
- **Household consumption patterns** (learned from historical energy meter data)
- **Battery characteristics** (capacity, power limits, efficiency curves, degradation)
- **Historical price model** (fallback and horizon extension when day-ahead prices are not yet published)

Battery Controller works with any battery inverter and electricity meter — it is a calculated integration that reads sensors and writes setpoints. It does not communicate directly with hardware.

![Battery Controller dashboard showing electricity prices, battery charge/discharge schedule and SoC](docs/assets/battery-with-prediction.png)

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

## Quick start

1. **Check your sensors first.** A price sensor needs forecast attributes; an energy
   sensor needs `state_class: total_increasing`. See
   [Installation](https://bvweerd.github.io/battery_controller/installation/).
2. **Install via HACS**: HACS → Integrations → three dots → Custom repositories → add
   `https://github.com/bvweerd/battery_controller` as an **Integration**. Restart Home
   Assistant.
3. **Add the integration**: Settings → Devices & Services → Add Integration → Battery
   Controller.
4. **Add your hardware as subentries**: at least one **Battery**, plus a **PV Array** per
   roof orientation.
5. **Wire it to your inverter** with an automation that reads
   `sensor.battery_controller_battery_setpoint`. See
   [Connecting your inverter](https://bvweerd.github.io/battery_controller/inverter-control/).

> **Give it 2–4 weeks.** The consumption pattern and historical price model learn from
> your recorder history, and the schedule stabilizes as they converge. See
> [the learning period](https://bvweerd.github.io/battery_controller/how-it-works/#learning-period-give-the-optimizer-time-to-calibrate).

---

## Contributing

Documentation lives in [`docs/`](docs/) and is published to GitHub Pages with MkDocs
Material on every push to `dev`. To preview locally:

```bash
pip install -r requirements-docs.txt
mkdocs serve
```

The diagnostics analyzer is a hand-written static app in [`docs/analyzer/`](docs/analyzer/),
with its Jest suite in `docs/__tests__/` (`npm test`).

## License

See [LICENSE](LICENSE) for details.

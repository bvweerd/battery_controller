# Project Architecture

## Directory structure
```
battery_controller/
├── custom_components/battery_controller/   # Integration source (domain = battery_controller)
│   ├── optimizer.py          # DP engine: backward induction, Bellman eq, oscillation filter
│   ├── coordinator.py        # 3 cascading DataUpdateCoordinators (30min→15min→15min)
│   ├── zero_grid_controller.py  # Real-time ~5s grid balance (PID-less direct calculation)
│   ├── forecast_models.py    # PV model, consumption pattern, historical price model
│   ├── battery_model.py      # Physics: RTE split, SoC limits, power constraints
│   ├── config_flow.py        # Setup + options flow (sections with collapsed panels)
│   ├── const.py              # SOC_RESOLUTION_WH=100Wh, POWER_STEP_W=500W, config keys
│   ├── sensor.py / number.py / select.py / switch.py / binary_sensor.py
│   ├── helpers.py            # Price extraction (Nordpool/ENTSO-E/generic formats)
│   ├── manifest.json         # domain, version, requirements: [aiohttp]
│   └── translations/         # en.json + nl.json (strings.json = copy of en.json)
├── tests/                    # pytest tests (one file per source module)
└── setup.cfg                 # pytest, flake8, isort, mypy, bumpversion config
```

## Coordinator cascade
1. **WeatherDataCoordinator** (30 min) — fetches solar radiation + wind from open-meteo.com
2. **ForecastCoordinator** (15 min) — PV production + consumption forecasts; depends on (1)
3. **OptimizationCoordinator** (15 min) — runs DP optimizer + zero-grid controller; depends on (2)

## Core algorithm (optimizer.py)
- State space: time steps × SoC states (100 Wh resolution) × power actions (500 W steps)
- Backward pass: `V[t][s] = min(step_cost + V[t+1][s'])` for all actions
- Forward pass: execute `best_action[t][current_soc_state]`
- Terminal condition: `V[T][s] = -(soc_kwh × feed_in_price_T)` — prevents horizon-end discharge
- Shadow price: `λ = -dV[0]/dSoC` — marginal value of stored energy (used in hybrid mode)

## Key external dependencies
- `aiohttp` — async HTTP for open-meteo.com weather API
- `pytest-homeassistant-custom-component` — HA test fixtures
- `syrupy` — snapshot testing
- `ruff` — linting + formatting (replaces flake8/black in pre-commit)

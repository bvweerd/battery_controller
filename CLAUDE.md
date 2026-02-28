# Battery Controller
Home Assistant custom integration that optimizes home battery charge/discharge scheduling using dynamic programming (backward induction) to minimize electricity costs.

## Commands
- **Tests**: `python -m pytest tests/ -v`
- **Type check**: `mypy custom_components/battery_controller/`
- **Lint**: `pre-commit run --all-files`
- **Install deps**: `pip install -r requirements.txt`

## Structure
- `custom_components/battery_controller/` — integration code
  - `optimizer.py` — DP engine (backward induction, Bellman equation)
  - `coordinator.py` — 3 cascading coordinators (Weather→Forecast→Optimization)
  - `zero_grid_controller.py` — real-time ~5s grid balance control
  - `forecast_models.py` — PV and consumption forecasting + historical price model
  - `battery_model.py` — battery physics (RTE split as √RTE per direction)
  - `config_flow.py` — setup + options flow with `section()` helpers
  - `const.py` — SOC_RESOLUTION_WH=100, power step=500W, all config keys
- `tests/` — pytest with `pytest-homeassistant-custom-component` + syrupy snapshots

## HA Conventions
- `_attr_has_entity_name = True` on all entities
- `_attr_translation_key` alongside `_attr_name` (fallback)
- `strings.json` = copy of `translations/en.json` (required by HA)
- `section()` imported from `homeassistant.data_entry_flow` (not helpers.selector)
- Use `async def` for all I/O; never synchronous I/O in async context

## Critical Implementation Notes
- **Feed-in price**: Never return `None` to optimizer — falls back to `CONF_FIXED_FEED_IN_PRICE` (€0.07). Returning None causes DP to default to grid_price, making PV arbitrage unprofitable.
- **RTE split**: `charge_eff = discharge_eff = sqrt(RTE)` — ensures `charge × discharge = RTE`
- **Oscillation filter**: Post-DP pass removes charge↔discharge pairs where spread < `(2×degradation + min_price_spread) / sqrt(RTE)`. Uses 2-hour lookahead window.
- **DC PV**: ~97% efficient (MPPT only); AC PV ~85% (through inverter). Excess DC goes to AC at 96%.
- **SoC fallback**: If SoC sensor unavailable, last known SoC from previous run is used.

## Compaction: always preserve
- Modified files and their key changes
- Test failure messages (full traceback)
- Current domain name (`battery_controller`) and version from manifest.json

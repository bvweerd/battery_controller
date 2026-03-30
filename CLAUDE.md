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
  - `const.py` — SOC_RESOLUTION_WH=25 (power_step derived as SOC_RES/step_h), all config keys
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

## After Every Code Change
Always do the following after modifying any source file:
1. **Run tests**: `python -m pytest tests/ -v` — fix any failures before proceeding
2. **Run pre-commit**: `pre-commit run --all-files` — fix any issues (ruff may auto-format)
3. **Update `ALGORITHM.md`** — if algorithm logic, efficiency model, or DP behaviour changed
4. **Update `simulate_diagnostics.py`** — if inputs, outputs, or data structures changed
5. **Update `docs/index.html`** — if the simulator UI or displayed parameters changed

## Keeping DP implementations in sync
There are three DP implementations that **must always be kept identical** in algorithm, constants, and cost semantics:
- `custom_components/battery_controller/optimizer.py` — the integration (source of truth)
- `docs/analyzer.js` — JS re-implementation used by the diagnostic simulator
- `simulate/simulate_diagnostics.py` — Python script for local diagnostics testing

When changing **any** of the following in `optimizer.py`, apply the same change to `analyzer.js` and `simulate_diagnostics.py`:
- SoC transition logic (charge/discharge/idle, RTE split, passive DC PV)
- `calculate_step_cost` cost formula (grid cost, degradation, DC PV path, grid cap)
- Action generation (power steps, charge/discharge limits)
- Terminal condition (terminal price, V[T] initialization)
- V[t][s] fallback for unreachable states
- Shadow price formula and sign convention
- Oscillation filter thresholds (min_price_spread, degradation formula)
- Constants: `SOC_RESOLUTION_WH`, `DC_TO_AC_INVERTER_EFFICIENCY`

## Compaction: always preserve
- Modified files and their key changes
- Test failure messages (full traceback)
- Current domain name (`battery_controller`) and version from manifest.json

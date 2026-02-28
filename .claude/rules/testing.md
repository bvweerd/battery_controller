---
paths:
  - "tests/**/*"
---

# Testing

## Framework
- `pytest` with `pytest-homeassistant-custom-component`
- `asyncio_mode = auto` — all tests are async by default
- `syrupy` for snapshot assertions
- `--maxfail=1 --strict --disable-warnings` configured in setup.cfg

## Run tests
```
python -m pytest tests/ -v
python -m pytest tests/test_optimizer.py -v   # single file
python -m pytest tests/ -v -k "test_name"     # single test
```

## Fixtures
- `hass` — HomeAssistant instance (from pytest-homeassistant-custom-component)
- `snapshot` — syrupy snapshot fixture
- Conftest: `tests/conftest.py`

## Patterns
- Mock external API calls (open-meteo.com) with `aioresponses` or `unittest.mock.AsyncMock`
- Use `async_setup_component` or config entry helpers to load the integration
- Each test module maps to a source module: `test_optimizer.py` → `optimizer.py`
- Snapshot tests: first run creates `.ambr` files; update with `--snapshot-update`

## Coverage
- Source: `tests/` (see setup.cfg `[coverage:run]`)
- Run with coverage: `python -m pytest tests/ --cov=custom_components/battery_controller`

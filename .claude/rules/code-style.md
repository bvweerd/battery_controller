---
paths:
  - "custom_components/**/*.py"
  - "tests/**/*.py"
---

# Code Style

## Formatter
- `ruff-format` (via pre-commit) — line length 88
- `isort` profile: multi_line_output=3, trailing comma, parentheses, line_length=88
- Known first-party: `custom_components`, `tests`

## Linter
- `ruff` with `--fix` (via pre-commit)
- `flake8` max-line-length=88; ignores: E501, W503, E203, D202, W504

## Type checking
- `mypy` targeting Python 3.13
- `ignore_missing_imports = true`; `follow_imports = silent`
- Add type hints to all public functions and method signatures

## HA-specific conventions
- `_LOGGER = logging.getLogger(__name__)` at module level
- `async def` for all I/O-bound operations — no blocking calls in async context
- Entity classes: `_attr_has_entity_name = True`, set `_attr_translation_key`
- Config flow: inherit from `config_entries.ConfigFlow`; options flow from `config_entries.OptionsFlowWithConfigEntry`
- Import `section()` and `SectionConfig` from `homeassistant.data_entry_flow`
- Use `coordinator.data` pattern; raise `UpdateFailed` on errors in `_async_update_data`

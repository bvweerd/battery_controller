---
name: validate-hacs
description: Validate the HACS integration for compliance
---

# Validate HACS

1. Check `manifest.json`: domain, name, version, requirements, codeowners, iot_class, config_flow
2. Check `hacs.json`: name, render_readme, zip_release, filename
3. Check file structure: `custom_components/battery_controller/__init__.py` present
4. Check `strings.json` is an exact copy of `translations/en.json`
5. Verify version in `manifest.json` matches `setup.cfg` `[bumpversion] current_version`
6. Check README.md is present at repo root
7. Run hassfest if available: `python -m script.hassfest`
8. Report issues and suggest fixes

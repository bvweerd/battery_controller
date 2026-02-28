---
name: run-tests
description: Run the full test suite and provide a summary
---

# Run Tests

1. Run: `python -m pytest tests/ -v --tb=short 2>&1`
2. Summary: how many tests passed/failed/skipped
3. On failure: show only the FAILED/ERROR lines with full traceback
4. For snapshot failures: suggest `python -m pytest tests/ --snapshot-update` to regenerate
5. Suggest fixes for repeated failures

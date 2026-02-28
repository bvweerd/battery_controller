---
name: fix-issue
description: Fetch a GitHub issue, analyze it, fix it, and open a PR
---

# Fix Issue #<number>

1. Fetch issue details: `gh issue view <number> --json title,body,labels,comments`
2. Analyze the issue and identify the relevant code area
3. Implement the fix in `custom_components/battery_controller/`
4. Write/update tests in `tests/`
5. Verify all tests pass: `python -m pytest tests/ -v`
6. Run pre-commit: `pre-commit run --all-files`
7. Commit with: `fix: resolves #<number> - <short description>`
8. Open a PR: `gh pr create --title "fix: #<number> <title>" --body "Fixes #<number>"`

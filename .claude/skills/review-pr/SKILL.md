---
name: review-pr
description: Thoroughly review a pull request
---

# Review PR <number>

1. Fetch PR diff: `gh pr diff <number>`
2. Fetch PR details: `gh pr view <number> --json title,body,files,reviews`
3. Analyze:
   - Correctness of the implementation
   - Test coverage (are there tests for the changes?)
   - Code style: ruff-format compliant, type hints present, async patterns correct
   - HA conventions: entity naming, coordinator pattern, translation keys
   - Critical invariants: feed-in price never None, RTE split as √RTE, oscillation filter formula
   - Potential bugs or edge cases
   - Breaking changes to entity IDs or config schema
4. Write review comments as a markdown list
5. Post comments via: `gh pr review <number> --comment --body "<your review>"`

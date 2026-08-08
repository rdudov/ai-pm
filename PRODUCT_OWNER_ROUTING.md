# Product-owner engine routing

All interactive, timer and mail entrypoints call
`scripts/claude_product_owner.py`; none pins a background model beside it.

- `claude-pm` starts the ordinary shared policy. Opus remains the default and
  Fable is selected only when the published Opus-specific remainder is below
  five percent.
- An observed exhausted shared Claude limit, or observed exhaustion of both
  Opus and Fable model-scoped routes, selects the same product owner through
  Codex `gpt-5.6-sol`.
- Network/API failures and unknown quota schemas never imply exhaustion. They
  keep Opus and make the failure visible.
- `codex-pm` explicitly selects the same owner through the pinned Codex reserve
  for manual use and runtime smoke. Both commands run from
  `/opt/projects/product-owner` with `/opt/projects` access.

Background callers may use `--entry print`; legacy Claude-shaped print argv is
still translated by the same router during rollout. `--status` prints only
redacted routing observations and `--show-command` shows the selected argv.

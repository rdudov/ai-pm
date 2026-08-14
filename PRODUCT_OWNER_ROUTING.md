# Product-owner engine routing

All interactive, timer and mail entrypoints call
`scripts/claude_product_owner.py`; none pins a background model beside it.

- `claude-pm` compares the observed shared seven-day Claude remainder with the
  latest observed seven-day Codex remainder and selects the family with more
  room. Within Claude, Opus remains the default and Fable is selected only when
  the published Opus-specific remainder is below five percent.
- An observed exhausted shared Claude limit, or observed exhaustion of both
  Opus and Fable model-scoped routes, selects Codex even before that comparison.
- A missing observation is never turned into a percentage. Without a comparable
  weekly remainder the router keeps Opus and names which observation is missing;
  Claude network/API failures and unknown schemas remain visible as before.
- The Codex side is accepted only from an explicitly 10,080-minute rate-limit
  window whose event timestamp still belongs to its unexpired reset window.
- The quota endpoint uses Claude Code's short-lived OAuth access token. If that
  endpoint returns 401, the router serializes recovery and asks Claude Code—the
  credential owner—to execute its built-in zero-turn `/usage` command, then
  retries the exact endpoint once. The router never implements refresh-token
  exchange itself and never logs credentials or the helper output.
- `codex-pm` explicitly selects the same owner through the pinned Codex reserve
  for manual use and runtime smoke. Both commands run from this repository, with
  read access to the task system it observes.

Background callers may use `--entry print`; legacy Claude-shaped print argv is
still translated by the same router during rollout. `--status` prints only
redacted routing observations: exact shared and explicitly model-scoped reset
windows, an explicit `published: false` when Opus or Fable has no separate
field, the latest Codex budget observation, the live observation time/source,
authorization-recovery provenance, and a typed authorization/network/schema
error when observation is unavailable.
Unavailable observation contains no fabricated remainder and keeps product work
on visible Opus behavior. `--show-command` shows the selected argv.

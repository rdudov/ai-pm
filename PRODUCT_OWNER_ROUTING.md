# Product-owner engine routing

All interactive, timer and mail entrypoints call
`scripts/claude_product_owner.py`; none pins a background model beside it.

- `claude-pm` explicitly selects Claude for its manual interactive session.
- `codex-pm` explicitly selects Codex for its manual interactive session.
- Manual and background Claude routes differ by model since 2026-09-23 (the
  user's variant B): the interactive console and the Gmail door (`--entry mail`,
  print-shaped) run on Fable, and `--entry print` (thread ticks, standup, goal
  sessions) runs on Opus, which the CLI alias resolves to Opus 5.5. The background gives most of the volume and mostly
  observes and launches; Opus 5.5 is declared Fable-level on most work at a
  lower price. Each entry keeps the other model only as its fallback, selected
  when the provider explicitly reports the primary exhausted. An observation
  that fails keeps the entry's primary model, so a network or authorization
  hiccup cannot quietly move a route.
- The Codex fallback of the product owner is GPT-6 Sol at effort `xhigh`, not
  Astra: it runs only when the Claude windows are gone, and then the Codex
  window belongs to the authors; Astra spends it five times faster.
- Unforced routes stay on Claude. They move to Codex only when the provider
  reports the shared Claude limit exhausted, or both Opus and Fable exhausted.
  The router no longer sends the product owner to Codex because Codex has the
  larger weekly remainder: the user put the product owner on Claude on
  2026-09-02 and kept Codex's window for the authors. Observed on 2026-09-28:
  with Claude at 65% and Codex at 82% every background tick went to Codex,
  answered `SILENT` in about ten seconds, and four tasks waiting for their next
  step stood overnight. Both remainders stay in the route reason.
- A missing observation is never turned into a percentage. Without a comparable
  weekly remainder the router keeps Claude and names which observation is missing;
  Claude network/API failures and unknown schemas remain visible. Every selected
  Claude route emits its reason before the engine starts: mail keeps it in its
  existing agent stderr artifact, and the timer forwards that one diagnostic to
  its service journal without replaying arbitrary model stderr.
- `--entry print` is the one entry whose caller is a service rather than a
  person, so it is the one entry the router stays alive above instead of
  replacing itself with the engine. It runs Claude Code under
  `--output-format json`, passes the envelope's own `result` to stdout — the
  same plain text callers already parse — and exits `75` (`EX_TEMPFAIL`) when
  that envelope says the provider refused before the model ever answered:
  `terminal_reason: api_error` with an empty `modelUsage` and every token
  counter at zero. A refusal that arrives after the model has worked has
  non-zero counters and is reported as an ordinary failure, because repeating
  such a request would repeat what it did. Output that is not an envelope is
  passed through byte for byte and never earns the code, and a child that exits
  75 for a reason of its own is reported as 1. The engine is bound to the
  router's own life with `PR_SET_PDEATHSIG`, so a caller that kills the router
  on timeout still kills the engine, as replacing the process used to. The mail
  door of the task-system repository is what reads exit 75: it returns that one
  conversation request to its waiting queue exactly once.
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
on the visible default model. `--show-command` shows the selected argv.

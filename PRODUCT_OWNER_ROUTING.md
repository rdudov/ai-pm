# Product-owner engine routing

All interactive, timer, mail, standup and goal-session entrypoints call
`scripts/claude_product_owner.py`.

The user's October 1, 2026 instruction switches every product owner to Codex
GPT-6.1 Sol (`gpt-6.1-sol`) with effort `xhigh`. This decision takes precedence
over legacy `--force-claude` and `--force-codex` compatibility flags: both
`claude-pm` and `codex-pm` run the same Codex owner.

Routing does not call the Anthropic usage endpoint or Claude authorization
recovery. `--status` reports the Claude observation as `not_requested` and
retains the latest observed Codex budget without inventing a remainder.
`--select` prints the model; `--show-command` prints the selected argv.

The existing Codex interactive and print execution paths are reused. Print
callers receive only model output on stdout and routing diagnostics on stderr.
Legacy Claude-shaped print arguments continue through that same translation.
The Claude quota helpers remain available for their direct callers but are not
part of the product-owner launch path.

Independent review temporarily uses Codex Astra (`gpt-6-astra`) at effort high
following the user's later October 1 instruction. Authors remain Codex Sol at
high; product owners remain Codex Sol at xhigh. Reviewer isolation is a fresh
read-only session under `isolated_same_provider`, not a different-provider
review. Companion selects the model and existing assurance strategy. Old author
bindings require ordinary author re-admission; previous records are preserved.

# Development and production on separate hosts

Keep the product owner's code, content, tasks, mail ledger and board together on
the development host. Production databases and services remain authoritative on
their own hosts. Inspect them with explicit SSH commands; a copied database or
local Git checkout establishes neither production health nor an installed version.
The board's timer state and buttons refer to the development host's real systemd
manager. Reach the localhost board over SSH rather than publishing its port.

Use the accepted Python version and exact task-engine pins. All entry points
(systemd, cron and SSH) need the same explicit guarded PATH, HTTP proxy and Python,
without relying on an interactive shell profile. Run system services with
`User=agent`. Privileged systemd control must drop back to that account before
starting a watcher or model. The task-system `systemd_scope_as_user.py` PATH
adapter elevates scope creation and runs the unchanged watcher as its original
non-root caller. Quota resume accepts `COMPANION_SYSTEMD_SUDO=1` for installation
and control and `COMPANION_SYSTEMD_ENV_FILE` for its runtime environment; generated
services record their caller's user. These installation options leave engine and
model policy unchanged.

The existing Companion integration provides optional SSH projection:

- Authenticated bot requests call the same mail door remotely with the original
  principal, event ID, request and attachment manifest. The mail ledger alone
  owns duplicates, waiting requests and the one product owner.
- Bot sends and destination authorization run on the bot host with its existing
  credentials; task-side claims and receipts remain on the development host.
- Product answers call the bot host's existing reply/history script through
  `remote_product_owner_reply.py`. Configure the mail door's
  `wake_reply_instruction` to name this adapter with the original event/principal.
  A bare bot send loses history and is not an equivalent answer path.
- Administrative Codex start/resume/recovery uses the existing trusted lifecycle
  on the development host. Its original callbacks execute in the real bot process.
  Terminal delivery is acknowledged after its callback succeeds; an interrupted
  claim retains the lifecycle's uncertainty policy. Protected file tasks and their
  per-user indexes stay on the bot host.

The task system's skills own the SSH settings and wire procedure; Companion's
Codex README owns its endpoint. Never copy bot credentials to the development host
or forward them through SSH environment settings.

Prepare units with schedules disabled. Name a timezone in OnCalendar, retaining
the system timezone. Preserve product pauses in durable content. Finish running
turns and drain the old owner before final consistent transfer of tasks, mail,
session state and SQLite WAL. Session files and PIDs do not transfer a process.
Preserve the new host's own login. Stop the old owner before enabling the new one.

Native acceptance exercises author/reviewer uid, independent scope survival,
network boundary, mail proxy, authenticated files, actual history, duplicate and
failure paths, board and recovery. Source tests do not establish installed facts.
Rollback first fences the new owner, then reconciles fresh ledgers and unfinished
work; old completed status cannot substitute for fresh state. Follow the existing
operator backup and cleanup policy.

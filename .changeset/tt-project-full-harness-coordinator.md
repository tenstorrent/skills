---
"tt-project": patch
---

`tt-project`: the persistent coordinator session runs Claude Code's full harness like a worker. A full-tools session wake (`coordinator.session.tools` "full", the default) appends its text to Claude Code's own system prompt (`--append-system-prompt-file`) instead of replacing it, gets Claude Code's whole standard tool set (no `--tools` list: the subagent tool, `Agent` or `Task`, among them, and Grep, Glob and TodoWrite where the build ships them), and drops `--restricted` and dontAsk for bypassPermissions, as workers do. The rules stay in the hook: the harness tools (cron, wake-ups, remote triggers, worktree switches, push notifications) and searches from `/` or the home folder are refused as for a worker, a subagent's calls meet the same checks, and the charter, memory, settings and state database still change only through actions. The web tools stay withheld under `no_internet`, and the project's `providers.claude.disallowed_tools` now binds the coordinator too. `read_only` sessions, Codex and Cursor are unchanged.

`tt-project`: each Claude run's note records the subagents it started (`subagents_spawned`, `subagent_types`, counted once per Agent call from its output; their cost stays in the run's own), and `ttp stats --landing` adds the booked spend per landed commit and the subagents spawned over the last 24 h and 7 d.

`tt-project`: fewer coordinator wakes. Relayed messages with no order or question wait up to `coordinator.relay_window_s` (90 s; 0: off) so a burst shares one turn, while an order, a question or the user's own message still wakes at the debounce; a slow tick step's reports never start a turn by themselves.

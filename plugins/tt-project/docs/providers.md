# Provider parity

What the runtime needs from each agent CLI, and what each one offers. Each cell says what the
adapter in `runtime/ttp/providers/` does and links the official doc it relies on.

Claude Code and the Codex CLI have been run live; Cursor has not. `ttp doctor --live <provider>
[--json]` measures one provider's row: in a scratch project it starts a small worker run the way
the daemon does, resumes its session, and runs one read-only coordinator turn with the
coordinator's structured-output schema. It costs a few tens of cents per provider. Each check is
pass, fail, or unsupported (the adapter does not use that feature; the note says why).

Measured on 2026-10-09:

| Check | Claude Code 2.1.285 | Codex CLI 0.160.0 | Cursor agent |
| :- | :- | :- | :- |
| Launch and login | pass | pass | not measured: CLI not installed |
| Session id | pass (assigned up front) | pass | not measured |
| Usage | pass (reported cost) | pass (estimated from tokens) | not measured |
| Structured output | pass | pass | not measured |
| Write fence | unsupported: `bypassPermissions` runs without the Bash sandbox, which would need bubblewrap | pass (refused: read-only file system) | not measured |
| Resume | pass | pass | not measured |
| Steer mid-run | pass (hook) | unsupported: no hook, the worker reads `steer.md` between steps | not measured |
| Plan meter | pass (five-hour and seven-day) | pass (seven-day) | not measured |

The steer row was measured before Codex workers got the harness hook. Codex was also run live on
codex-cli 0.160 for the hook and isolation rows (one worker run: an update handed over once, a
PR draft bypass refused), so where hooks are on, Codex now takes updates by hook as well.

The Codex structured-output check first failed: the API rejected the coordinator schema as
"invalid schema keyword" because an optional property that was already nullable got a second
`"null"` in its type list. The adapter now adds `"null"` only once.

The table below says what each adapter does and links the official doc it relies on. The Cursor
column was checked against its docs and against fixture tests built from the documented output
formats only. Flags that only some builds have are probed with `--help` first. A build without
them keeps the older behaviour.

| Feature | Claude Code (`claude -p`) | Codex CLI (`codex exec`) | Cursor agent (`agent -p`) |
| :- | :- | :- | :- |
| System prompt from a file | Yes. Workers: `--append-system-prompt-file`, or `--append-system-prompt` on older builds. Coordinator: `--system-prompt` replaces it. [cc-cli] | Yes: `-c developer_instructions="..."`, as a TOML string [cx-cli], for workers and the coordinator [cx-config]. It adds to Codex's built-in instructions; `model_instructions_file` would replace them, so it is not used. Text over 120 kB, too long for one argument, leads the prompt instead. | No flag [cu-params]. Rules come from `.cursor/rules`, `AGENTS.md` and `CLAUDE.md` [cu-using]. The system text leads the prompt. |
| Budget cap | Yes: `--max-budget-usd` [cc-cli]. | No flag [cx-cli]. The runner stops a run on its estimated spend, which counts completed turns only. | No flag [cu-params]. The runner stops a run on its estimated spend. Before any usage arrives, it uses a floor based on the text written. |
| Effort | Yes: `--effort` [cc-cli]. | Yes: `-c model_reasoning_effort=<level>` [cx-config]. | No flag [cu-params]. Reasoning depth is part of the model name, so tiers pick models. |
| Auto-compact | Yes: `CLAUDE_CODE_AUTO_COMPACT_WINDOW`. It accepts 100000 to 1000000 tokens and raises smaller values to 100000 [cc-env], so the adapter clamps to that range too. | Yes: `-c model_auto_compact_token_limit=<tokens>` [cx-config]. | Not used. Only the interactive `/summarize` command is documented [cu-using]. |
| Session id capture | Yes: `session_id` on the `system`/`init` event and the result [cc-headless]. | Yes: `thread_id` on the `thread.started` event [cx-exec]. | Yes: `session_id` on every stream event and on the result [cu-output]. |
| Resume | Yes: `--resume <id>` [cc-cli]. Transcripts are found under the run's own or the daemon's `CLAUDE_CONFIG_DIR` [cc-sessions]. | Yes: `codex exec [options] resume <SESSION_ID> -`, with the prompt on stdin [cx-cli]. Rollouts are saved by default [cx-exec]. They are found under `$CODEX_HOME/sessions/YYYY/MM/DD/rollout-*-<id>.jsonl`, a layout the docs do not state. | The argv is ready: `--resume <chatId>` [cu-params]. Runs are not resumed yet: the docs name no chat store, so a lost run starts fresh. |
| Usage and cost | Reported: the result carries `total_cost_usd` and usage [cc-cost]. | Tokens are on `turn.completed`, with no cost [cx-exec]. The cost is estimated from `pricing.codex`. | The documented events carry no usage or cost [cu-output]. Usage is read when a build reports it. Otherwise the run is booked at the elapsed share of its budget. |
| Read-only coordinator | `--restricted`, `--permission-mode dontAsk` and `--disallowedTools Edit Write NotebookEdit` [cc-cli]. | `-s read-only` [cx-cli]. The shell and web tools are off where `codex features list` names them. Runs start in a scratch directory. | `--mode ask`, where `--help` lists it, and no `--force` [cu-params]. Runs start in a scratch directory, away from the project rules. |
| Plugin loading | `--plugin-dir`, repeated [cc-cli]. | No per-run flag in the CLI reference [cx-cli]. Plugins load only from Codex's home, where `codex plugin add` copies them; a marketplace and `enabled` given by `-c` alone load nothing (measured on 0.160). So `plugin_dirs` is not used. Installed plugins load as usual. | `--plugin-dir`, repeated, where `--help` lists it [cu-params]. |
| Harness hook (updates, guards) | `--settings` with the hook, per run [cc-hooks]. | `-c hooks.PreToolUse=...` and `hooks.PostToolUse=...`, with `--dangerously-bypass-hook-trust`, since a hook given by `-c` is skipped untrusted (measured). Only when `codex features list` shows hooks on, `exec --help` lists the flag, and no other hook source exists (home `hooks.json` or `config.toml` hooks or plugins, a repository `.codex/`) [cx-hooks]. Same payload and replies as Claude Code. Otherwise `steer.md` is read between steps. | None: `steer.md` is read between steps. |
| Isolation from user config | `worker_isolation`: `--setting-sources project,local`, `--strict-mcp-config` and the listed MCP servers [cc-cli]. | `worker_isolation` and every coordinator turn: `--ignore-user-config` (sign-in still works); coordinator turns also `--ignore-rules`. Probed in `exec --help`; skipped when the user config sets a model provider. No per-run MCP list. | None. |

The project chooses which plugin directories load, per provider, in
`providers.<name>.plugin_dirs`.

## Open gaps

Measured on 2026-10-09 with Claude Code 2.1.285 and Codex CLI 0.160.0; rerun
`ttp doctor --live <provider>` after a CLI upgrade.

- Claude Code: workers have no write fence. They run with `bypassPermissions`, and the Bash
  sandbox that would fence them needs bubblewrap.
- Codex: the harness hook runs only where hooks are on and no other hook source exists.
  Otherwise a worker sees an update only when it reads `steer.md` between steps.
- Codex: the docs give no version for `developer_instructions` or
  `model_auto_compact_token_limit`. A build that ignores an unknown `-c` key would run a worker
  without its instructions or compaction.
- Codex: the cost is estimated from tokens and `pricing.codex`; the CLI reports no cost.
- Codex: where rollouts are kept is not documented. If it changes, lost runs start fresh.
- Codex: hook-source detection reads `config.toml` with simple patterns, so it errs towards
  seeing a source and leaving the hook off.
- Cursor: not measured, because the CLI is not installed on the test machine. From its docs: no
  chat store, so `session_saved` stays false and no run is resumed; no budget, effort,
  auto-compact or system-prompt switch; and no usage in the documented output.

[cc-cli]: https://code.claude.com/docs/en/cli-reference
[cc-env]: https://code.claude.com/docs/en/env-vars
[cc-headless]: https://code.claude.com/docs/en/headless
[cc-sessions]: https://code.claude.com/docs/en/sessions
[cc-hooks]: https://code.claude.com/docs/en/hooks
[cc-cost]: https://code.claude.com/docs/en/agent-sdk/cost-tracking
[cx-exec]: https://developers.openai.com/codex/noninteractive
[cx-cli]: https://developers.openai.com/codex/cli/reference
[cx-config]: https://developers.openai.com/codex/config-reference
[cx-hooks]: https://developers.openai.com/codex/hooks
[cu-params]: https://cursor.com/docs/cli/reference/parameters
[cu-output]: https://cursor.com/docs/cli/reference/output-format
[cu-using]: https://cursor.com/docs/cli/using

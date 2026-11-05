# Provider parity

What the runtime needs from each agent CLI, and what each one offers. Each cell says what the
adapter in `runtime/ttp/providers/` does and links the official doc it relies on.

Claude Code and the Codex CLI have been run live; Cursor is installed but has not run live (no login). `ttp doctor --live <provider>
[--json]` measures one provider's row: in a scratch project it starts a small worker run the way
the daemon does, resumes its session, and runs one read-only coordinator turn with the
coordinator's structured-output schema. It costs a few tens of cents per provider. Each check is
pass, fail, or unsupported (the adapter does not use that feature; the note says why).

`ttp doctor --loop <provider> [--json]` runs the whole path once: one light code task (add a line
to `NOTES.md`) in a scratch project whose only remote is a local bare repository, with one daemon
process of its own ticking until the task is dispatched, handed off, reviewed and pushed to that remote by
the push queue. It prints one line per step with the spend so far and stops at $1 or 15 minutes.
The coordinator does not run. A missing or logged-out CLI is reported with exit 0.

Claude Code measured on 2026-11-02, Codex CLI on 2026-10-09:

| Check | Claude Code 2.1.296 | Codex CLI 0.160.0 | Cursor agent |
| :- | :- | :- | :- |
| Launch and login | pass | pass | not measured: installed, logged out |
| Session id | pass (assigned up front) | pass | not measured |
| Usage | pass (reported cost) | pass (estimated from tokens) | not measured |
| Structured output | pass | pass | not measured |
| Write fence | pass on 2.1.296 (refused: read-only file system): the Bash sandbox where it can start (Linux with bwrap and socat, macOS); `providers.claude.write_fence` false turns it off; ssh, scp, sftp, rsync, git's network commands (fetch, pull, push, ls-remote, clone, remote and submodule update), `ttp lock`, `ttp machines` and `ttp devq` run outside it by name | pass (refused: read-only file system) | off: the sandbox denies network by default and no CLI flag allows it, so workers run with `--force` alone; on under `no_internet`, built, not measured live |
| Resume | pass | pass | not measured |
| Steer mid-run | pass (hook) | unsupported: no hook, the worker reads `steer.md` between steps | not measured |
| Plan meter | pass (seven-day; the run reported no five-hour window) | pass (seven-day) | not measured |

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
| System prompt from a file | Yes. Workers: `--append-system-prompt-file`, or `--append-system-prompt` on older builds. Coordinator: `--system-prompt-file` replaces it, or `--system-prompt` on older builds. [cc-cli] Inline text over 120 kB, too long for one argument, leads the prompt instead; an agent that cannot be started at all (E2BIG, a missing binary) ends as a failed run with one observation, never a lost one. | Yes: `-c developer_instructions="..."`, as a TOML string [cx-cli], for workers and the coordinator [cx-config]. It adds to Codex's built-in instructions; `model_instructions_file` would replace them, so it is not used. Text over 120 kB, too long for one argument, leads the prompt instead. Each run checks that its rollout's first 200 lines hold a developer message starting with the text; a task run without it is stopped (its spend counts as waste) and its task blocked, no attempt spent, with one observation naming the CLI version; a coordinator run is only reported. A rollout not found within 120 s, or with no model reply by then, is only reported, and not checked again. | No flag [cu-params]. Rules come from `.cursor/rules`, `AGENTS.md` and `CLAUDE.md` [cu-using]. The system text leads the prompt. |
| Budget cap | Yes: `--max-budget-usd` [cc-cli]. | No flag [cx-cli]. The runner stops a run on its estimated spend, priced from the running token total in its session rollout (`token_count` events) mid-turn. A cut-off run books that token price when its rollout can be read. | No flag [cu-params]. The runner stops a run on its estimated spend. Before any usage arrives, it uses a floor based on the text written. |
| Effort | Yes: `--effort` [cc-cli]. | Yes: `-c model_reasoning_effort=<level>` [cx-config]. | No flag [cu-params]. Reasoning depth is part of the model name, so tiers pick models. |
| Auto-compact | Yes: `CLAUDE_CODE_AUTO_COMPACT_WINDOW`. It accepts 100000 to 1000000 tokens and raises smaller values to 100000 [cc-env], so the adapter clamps to that range too. | Yes: `-c model_auto_compact_token_limit=<tokens>` [cx-config]. | Not used. Only the interactive `/summarize` command is documented [cu-using]. |
| Session id capture | Yes: `session_id` on the `system`/`init` event and the result [cc-headless]. | Yes: `thread_id` on the `thread.started` event [cx-exec]. | Yes: `session_id` on every stream event and on the result [cu-output]. |
| Resume | Yes: `--resume <id>` [cc-cli]. Transcripts are found under the run's own or the daemon's `CLAUDE_CONFIG_DIR` [cc-sessions]. | Yes: `codex exec [options] resume <SESSION_ID> -`, with the prompt on stdin [cx-cli]. Rollouts are saved by default [cx-exec]. They are found under `$CODEX_HOME/sessions/YYYY/MM/DD/rollout-*-<id>.jsonl`, a layout the docs do not state. | The argv is ready: `--resume <chatId>` [cu-params]. Runs are not resumed yet: the CLI keeps chats, but `ls` needs a terminal and the docs name no chat store, so a lost run starts fresh. |
| Usage and cost | Reported: the result carries `total_cost_usd` and usage [cc-cost]. | Tokens are on `turn.completed`, with no cost [cx-exec]. The cost is estimated from `pricing.codex`. | The documented events carry no usage or cost [cu-output]. Usage is read when a build reports it. Otherwise the run is booked at the elapsed share of its budget. |
| Read-only coordinator | `--restricted`, `--permission-mode dontAsk` and `--disallowedTools Edit Write NotebookEdit` [cc-cli]. With `coordinator.session.mode` "on" (coordtools.py): `--tools Read,Bash`, `--allowedTools` for the project folder and a fixed list of read-only `ttp` commands (`ttp hostcheck` among them), and a PreToolUse hook that denies everything else; `--safe-mode` is left out there, since it turns hooks off. | `-s read-only` [cx-cli]. The shell and web tools are off where `codex features list` names them. Runs start in a scratch directory. | `--mode ask`, where `--help` lists it, and no `--force` [cu-params]. Runs start in a scratch directory, away from the project rules. |
| Plugin loading | `--plugin-dir`, repeated [cc-cli]. | No per-run flag in the CLI reference [cx-cli]. Plugins load only from Codex's home, where `codex plugin add` copies them; a marketplace and `enabled` given by `-c` alone load nothing (measured on 0.160). So `plugin_dirs` is not used. Installed plugins load as usual. | `--plugin-dir`, repeated, where `--help` lists it [cu-params]. |
| Harness hook (updates, guards) | `--settings` with the hook, per run [cc-hooks]. | `-c hooks.PreToolUse=...` and `hooks.PostToolUse=...`, with `--dangerously-bypass-hook-trust`, since a hook given by `-c` is skipped untrusted (measured). Only when `codex features list` shows hooks on, `exec --help` lists the flag, and no other hook source exists (home `hooks.json` or `config.toml` hooks or plugins, a repository `.codex/`) [cx-hooks]. Same payload and replies as Claude Code. Otherwise `steer.md` is read between steps. | None: `steer.md` is read between steps. |
| Host loopback from the sandbox | pass on 2.1.296: the Linux Bash sandbox runs each command in a network namespace of its own (bwrap `--unshare-net`), so host loopback ports (an ssh `HostName 127.0.0.1` with a `Port`, a `LocalForward` end, a reverse tunnel's listening port, a local web server) refused connections; each fenced command now gets forwarders on its own 127.0.0.1 for the host's loopback listeners and the ssh config's loopback ports, through the sandbox's proxy, for the command's whole life (compound commands and scripts too). `providers.claude.loopback_ports` adds ports or, `false`, turns it off. Not forwarded: ports below 1024, the name `localhost` (it does not resolve inside: use 127.0.0.1), and macOS, whose sandbox has no namespace of its own. Still blocked: run it as a bare `ssh` or `ttp lock` command, or as a daemon-run command schedule, both outside the sandbox. Before/after on 2.1.296: refused (curl exit 7), then HTTP 200 | unchanged, not measured: its workspace-write sandbox runs with `network_access=true` | n/a: no write fence outside `no_internet` |
| Isolation from user config | `worker_isolation`: `--setting-sources project,local`, `--strict-mcp-config` and the listed MCP servers [cc-cli]; a listed claude.ai connector drops `--strict-mcp-config` and denies every other known server by name (`deniedMcpServers`, `--disallowedTools mcp__<name>`), never one on `mcp_withheld`. The `chat` provider (Claude Code on an OpenAI-compatible endpoint) takes claude's `worker_isolation` and `mcp_withheld` while its own are unset. | `worker_isolation` and every coordinator turn: `--ignore-user-config` (sign-in still works); coordinator turns also `--ignore-rules`. Probed in `exec --help`; skipped when the user config sets a model provider. `mcp_servers`: the listed `.mcp.json` servers as one `-c mcp_servers={...}` table, secrets through the environment (`env_vars`, `bearer_token_env_var`), which the worker's shell can read (Codex's default `shell_environment_policy` drops only `*KEY*`, `*SECRET*` and `*TOKEN*` names), unlike Claude's; a server whose env would change a variable the worker already has (`PATH`, `TTP_*`, the device mask) is not given; without isolation every other `config.toml` server gets `enabled=false` (codex-cli 0.160, `codex mcp list --json`). | None. |
| Tool denylist for workers | `--tools` with the built-in tools workers use (the web tools only where the task needs them), and `--disallowedTools` with `disallowed_tools` (e.g. `mcp__<server>__*`). A build without `--tools` gets the harness's default denylist instead [cc-cli]. | None: `disallowed_tools` is not used. | None: `disallowed_tools` is not used. |

The project chooses which plugin directories load, per provider, in
`providers.<name>.plugin_dirs`.
Extra directories inside the project root that fenced workers may write go in
`providers.<name>.write_dirs` (see the guide); the fence stays on for the rest.

## Open gaps

Claude Code 2.1.296 was measured live on 2026-11-02 ($0.07; 2.1.285 on 2026-10-09 gave the same
checks, with a five-hour window as well), Codex CLI 0.160.0 on 2026-10-09. A flag probe cannot see
a CLI that changed what it does, so the daemon reads each installed, logged-in provider's
`--version` at most hourly, model-free. When it differs from the last measured one, it runs
`ttp doctor --live <provider> --json --max-usd 0.50` once, detached, at most once per provider a
day for the host (a second change that day is measured the next day). The row is kept under
`cli_watch/` in the tt-project home, and a failing check or a missing row is one low observation.
On 2026-10-21 `ttp doctor --loop` passed
with Claude Code 2.1.296 (landed after review in 31 s, $0.09) and Codex CLI 0.160.0 (66 s,
$0.21). Cursor agent: installed, not measured (no login on the test machine). On 2026-11-02 it
passed again for Claude Code with workers in the write fence (38 s, $0.12).

- Any provider: a code task's raw `git push` to the push branch is refused by the harness's
  pre-push hook (`runtime/ttp/gitguard.py`) while `delivery.code_tasks_may_push` is off, and
  `ttp push` refuses the same landing. Before that guard, on 2026-10-21, a Claude Code worker whose
  task said to land its change pushed it straight to the remote; `ttp doctor --loop` still fails
  such a landing.
- A code task's plain `needs_review` hand-off gets its review from the daemon's review sweep,
  `review.sweep_after_s` (default 5 min) after its run ended; a `done` hand-off gets it at once.
- Claude Code: a host without bubblewrap or socat (Linux) runs workers unfenced and raises one
  low issue. The fence frees ssh and its kin by name only as a simple command on its own (also
  under `timeout N` or `ttp lock <res> --`). Any of these keeps the whole line fenced (measured):
  `&&`, `;` or a pipe, also after `ttp lock <res> -- ssh ...`; a redirect to or from a file (`<`,
  `<<`, `> file`, `2>/dev/null`; `2>&1` is fine); `$(...)` or a backtick anywhere in the line, even
  inside quotes; ssh inside `bash -c` or `eval`; a script that runs ssh. There host names do not
  resolve, and 127.0.0.1 is the sandbox's own loopback: it reaches only the host ports forwarded
  into it (see host loopback above), so a refused port there is a sandbox limit, never a sign that
  a host or tunnel is down. The hook refuses these forms. The workaround: a pipe goes
  inside the quoted remote command (`ssh host 'cmd | tail -20'`), anything else into a local
  wrapper script (e.g. `ssh host 'bash -s' < remote.sh > out.txt`) run as
  `ttp lock <res> -- bash wrapper.sh`; `ttp lock <res> -- bash -c '...'` also works, while a plain
  `bash wrapper.sh` stays fenced.
  git over an SSH remote (`git@host:...`, `ssh://`) is the same: `git fetch ...` on its own reaches
  the host (measured, `git ls-remote`), but not inside a compound command or after `git -C`, which
  the hook refuses. It tells an explicit URL's kind from the URL, else refuses when any remote of
  the repository uses SSH, even if the command names an HTTPS one. Still failing against an SSH
  remote: ttp commands and scripts that fetch or push internally, which run in the sandbox.
- Cursor: outside `no_internet` projects, workers run with `--force` alone and no write fence;
  `ttp doctor` names the reason.
  The sandbox would cut them off the network: in build `2026.10.22-27dcf5f` (read from its bundled
  code, not run live) its network policy denies by default, plus an allowlist the server sends.
  Only `sandbox.networkAccess` in the CLI config or an allow list in `.cursor/sandbox.json` opens
  it, and no CLI flag does, so git fetch, gh and ssh would fail. The harness writes neither file.
  `writable_args` keeps the built path (`--sandbox enabled` and one `--add-dir` per writable root,
  when `agent --help` lists both flags and the host can run the sandbox: macOS; Linux 6.2+ with
  unprivileged user namespaces allowed). It is used today only in projects with the `no_internet`
  restriction, where the blocked network is wanted (Codex likewise leaves its network off there).
  On Linux the sandbox helper (read from the same build) runs the worker in a network namespace of
  its own with a private loopback, so the host's loopback services are out of reach. Workers do not need them: `ttp` commands and the hand-off write files under the
  project state, which `--add-dir` keeps writable.
- Claude Code, known limitation: a mount race in the write fence (Linux, 2.1.296). For each fenced
  Bash command the CLI mounts a placeholder over the repository's missing `.git/config.lock`,
  which creates an empty file there, and deletes it afterwards. A sandbox that starts meanwhile
  sees the file, it is gone by the time bwrap mounts it, and the command fails with
  `bwrap: Can't get type of source path ...: No such file or directory`. tt-project cannot change
  this; its hook tells the worker the failure is a transient mount race and to re-run the same
  command unchanged. In a repro with 8 sandboxes running 60 commands each at once, 17-23% of
  commands failed this way. Adding or removing a worktree in the shared `.git/worktrees` while
  sandboxes start fails them the same way (88-92% of commands in that repro), so tt-project's own
  short-lived checkouts (`ttp checks`' clean checkout, after_push) are scratch clones that borrow
  the repository's objects, not worktrees: with them no command failed on a worktree path.
- Codex: the harness hook runs only where hooks are on and no other hook source exists.
  Otherwise a worker sees an update only when it reads `steer.md` between steps.
- Codex: the docs give no version for `developer_instructions` or
  `model_auto_compact_token_limit`. A build that ignores an unknown `-c` key would run a worker
  without its instructions or compaction.
- Codex: the cost is estimated from tokens and `pricing.codex`; the CLI reports no cost.
- Codex: where rollouts are kept is not documented. If it changes, lost runs start fresh.
- Codex: hook-source detection parses `config.toml` (a top-level `hooks` table or key, or a
  plugin not set `enabled = false`). Without `tomllib` (Python before 3.11) or `tomli`, or on a
  file that does not parse, it falls back to simple patterns, which err towards seeing a source
  and leaving the hook off. Not yet checked live.
- Cursor: installed in user space on 2026-11-01 (PST) with the documented installer
  (`curl https://cursor.com/install`, reviewed before it ran; it needs no sudo and writes only
  under `~/.local`: the build in `~/.local/share/cursor-agent/versions/` and the `agent` and
  `cursor-agent` links in `~/.local/bin`), build `2026.10.22-27dcf5f`. Not run live yet: the test
  machine has no Cursor login.
  - Version: `agent --version` prints the build alone; `ttp doctor --live` reads its last line.
  - Sign-in: `agent status` (also `agent whoami`) prints "Not logged in" and still exits 0 when
    logged out, so `login_check` reads the text as well as the exit code. A read-only shell check
    that exits 0 only when signed in:
    `out=$(timeout 30 agent status 2>&1) && test -n "$out" && ! printf '%s\n' "$out" | grep -qiE 'not (logged|authenticated)|login required|unauthenticated' || exit 1`.
  - Logged out: `ttp doctor --live cursor` and `ttp doctor --loop cursor` report "not available:
    logged out (run `agent login` there)" and start no run. A run that fails with Cursor's
    "Authentication required" error ends as `auth`: the task goes back to the queue without
    using an attempt, the provider's auth breaker stops new Cursor runs, one alert names
    `agent login`, and the daemon's model-free login check (`agent status`, backing off) lets
    runs start again once it passes.
  - Resume: the CLI keeps chats; its `--help` and docs list `--resume [chatId]`, `--continue`,
    `resume` (the latest chat), `create-chat` (prints a new chat's id) and `ls`. But `ls` is an
    interactive picker that fails without a terminal ("Raw mode is not supported"), no command
    lists chats for a script, and the docs name no place chats are kept. The harness cannot check
    that a chat still exists, so `session_saved` stays false and a Cursor run cut short by a reboot
    starts fresh (resuming blind could open an empty chat); `resume_args` builds `--resume <id>`
    for when a check exists. Revisit after a live run shows where chats are kept.
  - No budget, effort, auto-compact or system-prompt switch.

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

## Coordinator prompt cache evidence

The status miss threshold is **strictly below 50%** of prompt tokens read from cache.
The token-hit denominator is fresh input + cache writes + cache reads; output tokens do not
contribute. Rates aggregate token counters, rather than averaging each turn's percentage.
Zero means 0% read, partial means between 0% and 100%, and full means 100% read. Missing
cache counters, legacy runs without completeness metadata, and zero prompt denominators are
unknown, never misses. Raw counters remain available in `coordinator_cache.raw_tokens` in the
status API and audit JSON. Missing HTTP request counts also stay unknown: CLI turn counts
and assistant-message IDs do not prove how many transport retries occurred.

A local API-stub capture with Claude Code 2.1.296 observed three CLI breakpoints per request
(2.1.285 used four on later requests and rejected an extra mark with HTTP 400). Giving the
spare one to the coordinator's charter-and-memory block was tried and measured over about a
day and a half of coordinator turns: cost per turn rose at both low and high effort and the
token hit rate barely moved. The adapter therefore reserves all four for the CLI; charter and
memory end the system prompt, and the cache telemetry above stays to judge any later attempt.

The whole coordinator system prompt (rules, role, charter, STANDING and MEMORY) is a snapshot.
It is re-rendered only when the gap since the last turn exceeds `coordinator.cache_ttl` (the
cache is cold anyway), when it is older than `coordinator.prompt_max_age_s` (default 4 h), or at
once when the Restrictions (charter or restriction memory) change or a charter edit is made on
the user's word. Between refreshes, memory, deploys and other charter edits leave it
byte-identical: the digest carries memory added or retired, charter sections changed since the
snapshot, and a line naming a newly installed release whose rules apply at the next refresh.
The system MEMORY block holds only standing entries (restrictions, preferences, resources and
entries marked standing) in stable order; recent decisions and facts are listed in the digest.
Neither interval proves the provider's retention lifetime.

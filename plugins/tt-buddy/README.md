# tt-buddy

A coding agent with Tenstorrent operating principles.

```bash
/plugin install tt-buddy@tenstorrent-skills     # Claude Code
codex plugin add tt-buddy@tenstorrent-skills    # Codex
```

- Takes notes all the time.
- Learns the codebase when needed.
- Keeps the diff minimal.
- Strict, concise voice: simple English, short bullets.
- Routes device runs through `tt-device-mcp`.

## Skills

| Skill | Does |
|---|---|
| `tt-buddy:buddy` | Dispatch table, tone, and writing rules |
| `tt-buddy:run` | Run and test on device through the queue |
| `tt-buddy:learn` | Research the codebase; write a dated note |
| `tt-buddy:note` | Record findings to the notes timeline |
| `tt-buddy:skill-creator` | Design, build, and audit tt-buddy skills |

- Skills are selected automatically. You can also name one.
- Notes live in `~/.tt-buddy/notes/`, one git commit per entry.

## Requirements

- A tt-metal, vllm-tt-plugin, or tt-inference-server checkout.
- For device runs: a Tenstorrent device and `tt-device-mcp`.
- Setup steps: [`recipes/developer-setup.md`](recipes/developer-setup.md).
- Optional, for skill evals: a generic skill-creator.
  - Codex: built in.
  - Claude Code: install the separate `skill-creator` plugin.

## Hooks

- Session start loads the `buddy` primer.
- Every prompt reloads the tone. Prevents drift in long sessions.
- Run only in a TT context:
  - `/dev/tenstorrent` exists, or
  - a `tenstorrent/*` git checkout at, below, or beside cwd.
- Codex runs plugin hooks only after you trust them.
  - First start after install: "Hooks need review" → "Trust all and continue".
  - Later: `/hooks`, then press `t` to trust all.
  - `codex exec` skips untrusted hooks silently.
  - Untrusted hooks: the tone loads only when `buddy` runs.

## MCP servers

| Server | Registered by |
|---|---|
| `deepwiki` | This plugin, in both host manifests |
| `tt-device-mcp` | The tt-device-mcp installer (Claude Code) |
| `tt-device-mcp` | `codex mcp add tt-device-mcp -- tt-device-mcp` (Codex) |

## Check it works

Ask the agent:

```text
What tt-buddy skills do you have?
```

- It lists the five skills above.
- A "How does X work in tt-metal?" question routes to `tt-buddy:learn`.

## Layout

```
.claude-plugin/   .codex-plugin/   host manifests, deepwiki server
hooks/                             session-start, user-prompt-submit
recipes/<repo>/                    build, test, env, server steps
skills/<name>/                     SKILL.md plus sub-files
```

---
name: buddy
description: Use when starting any tt-metal, tt-inference, or vllm-tt-plugin task — a coding agent with Tenstorrent operating principles. Lists the tt-buddy skills, when to invoke each, and the artifacts they produce.
metadata:
  layer: meta
---

<EXTREMELY-IMPORTANT>
If you work in tt-metal, tt-inference-server, or vllm-tt-plugin, and a tt-buddy skill has even a 1% chance to apply: you MUST invoke it (§ Host mapping).

This is not optional. You cannot rationalize your way out of it.
</EXTREMELY-IMPORTANT>

## The Rule

- **Invoke the matching skill BEFORE any response or action.**
- A 1% match is enough.
- Wrong skill for the situation: you are released.
- NEVER skip the check.

## Dispatch Table

Invoke per § Host mapping.

| Task signal | Skill | Layer |
|---|---|---|
| Run/test on device, `pytest`, vllm-tt-plugin server lifecycle | `tt-buddy:run` | tool |
| "How does X work" / "what are the knobs for Y" | `tt-buddy:learn` | meta |
| Finding, observation, plan, or status update | `tt-buddy:note` | meta |
| Create, edit, or audit a tt-buddy skill | `tt-buddy:skill-creator` | meta |

No row matches: not tt-buddy work. Use default tools.

## Plugin root

- `<plugin-root>` is the tt-buddy plugin directory.
- The session hook states its absolute path.
- No hook output: two directories above this skill's base directory.
- Resolve every `<plugin-root>/...` path against it.

## Repo recipes

- `<plugin-root>/recipes/<repo>/` holds build, test, env steps.
- **MUST Read** the matching recipe before acting.
- NEVER use `cmake`, `ninja`, `make`, or direct C++ binaries.

## Layers

| Layer | Role | Examples |
|---|---|---|
| `tool` | Pipeline-bound, does one concrete thing | `tt-buddy:run` |
| `meta` | Cross-cutting, callable from any skill | `tt-buddy:learn`, `tt-buddy:note`, `tt-buddy:skill-creator`, `tt-buddy:buddy` |

## Red Flags

These thoughts mean STOP:

| Thought | Reality |
|---|---|
| "I'll just grep the codebase quickly" | Use `tt-buddy:learn`. It writes a reusable note. |
| "I'll put my notes in chat" | Use `tt-buddy:note`. Future sessions resume from notes. |
| "Not worth a note" | Note it. Notes are cheap; lost findings are not. |
| "I'll run pytest directly" | Use `tt-buddy:run`. Bare pytest skips the device queue. |
| "I'll run pkill / tt-smi -r myself" | Use `tt-buddy:run` recovery. Wrong order wedges the device. |
| "I'll also clean this up" | NEVER. Keep the diff minimal. |
| "This is a simple question" | Questions are tasks. Check the table. |
| "Let me explore first" | Skills tell you how to explore. Check first. |
| "I remember this skill" | Skills change. Load the current version. |
| "I'll skip the discipline once" | The discipline is the value. |

## Artifacts

- All work product lives in `~/.tt-buddy/notes/`.
- It is a git-tracked timeline, one file per topic.
- Write entries only via `tt-buddy:note`.
- Notes point to source-repo branches and commits.
- Branch names: `<user>/<workflow>/<scope>-<date>`.
- Skills NEVER push commits.

## Host mapping

| Action | Claude Code | Codex |
|---|---|---|
| Invoke a skill | Skill tool | Read its whole `SKILL.md`, then follow it |
| Subagent | `Agent`, `subagent_type=general-purpose` | Spawn a subagent; none available: run inline |
| MCP tool `<tool>` on server `<server>` | `mcp__<server>__<tool>`; plugin server: `mcp__plugin_tt-buddy_<server>__<tool>` | `<tool>` from `<server>` |

## Skill Invocation Pattern

When a skill says "invoke `tt-buddy:learn`" or similar:

1. **Invoke per § Host mapping.** NEVER paraphrase the skill.
2. **Pass the args** the skill specifies.
3. **Wait for its output** before the next step.
4. **Record the result** via `tt-buddy:note` when required.

## Always-on writing rules

Apply to every artifact: chat, commits, code.

- **Tone & Voice:** `tone.md`. The session hook loads it every prompt. No hook output: MUST Read it now.
- **Commit Messages:** before any `git commit`, MUST Read `commit-messages.md`.
- **Code Comments:** before editing source code, MUST Read `code-comments.md`.

Paths are relative to this skill's directory.

## Source of Truth

- Canonical content: the installed `<plugin-root>/skills/<name>/`.
- NEVER fetch skill content from the web.
- Installed skill unreadable: stop and report.
- A skill conflicts with this primer: the skill wins.
- User instructions (CLAUDE.md, AGENTS.md, messages) override everything.

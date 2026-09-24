---
name: skill-creator
description: "Design and build skills for the tt-buddy system — guides through rigorous design alignment before writing, then applies TT conventions. Use when creating, improving, or reviewing tt-buddy skills"
metadata:
  layer: meta
---

# TT Skill Creator

## Purpose

- Designs and builds tt-buddy skills.
- Most important part: **design the skill well**.
- Expose spec flaws. Reach full alignment before writing.
- Wraps `/skill-creator` for base mechanics: format, frontmatter, evals.
- Adds TT rules from `placement.md`, `prose.md`, `workflow.md`, `economy.md`, `self-check.md`.

## When to Invoke

Invoke `tt-buddy:skill-creator` when:

- The user asks for a new tt-buddy skill.
- The user asks to improve, review, or audit one.
- The user wants a convention check on a skill.

- NEVER edit a skill file without first invoking this skill.
- Bypassing skips the design gate, dedup sweep, self-check.
- Applies to "small" edits too.

## Pipeline

```
design → align → write → optimize → validate
```

- Design is the real work.
- Optimize catches bloat and scatter before shipping.

## Phase 1: Design Alignment

**Hard gate: present a design summary. Get explicit approval first.**
No exceptions, even with a full spec.

### Step 1 — First output (always)

Before any question, produce a design summary:

1. **Purpose:** what it does and does not do.
2. **Layer:** workflow / tool / meta, with justification.
3. **Trigger:** request patterns; boundary with adjacent skills.
4. **Input → output:** what it receives, produces, where output lands.
5. **Dependencies:** skills it calls, callers, device access.
6. **Open questions:** what context cannot resolve.

End with: _"What did I get wrong? What did I miss?"_
Wait for the developer's response.

### Step 2 — Interrogate gaps

- Ask one question at a time. Wait for the answer.
- Push back on vague answers. Ask for metric, target, loop.
- After each answer, restate your understanding.

### Step 3 — Expose flaws before approval

- Scope creep: 5 things may be 2 skills.
- Overlap: read the dispatch table.
- Volatile info MUST go through `tt-buddy:learn`, not assumed.
- Missing convergence criteria (workflow) or verification steps (tool).

### Step 4 — Gate

- All questions resolved: say _"I'm ready to write. Shall I proceed?"_
- **Do not write until the developer says yes.**

## Phase 2: Write

Only after explicit approval:

1. **Invoke `/skill-creator`** for base mechanics.
2. **Load** `placement.md`, `prose.md`, `workflow.md`, `economy.md` as needed.
3. Write SKILL.md + sub-files.
4. Keep the diff minimal. Touch only what the task needs.

## Phase 3: Optimize (gate)

Applies to new skills and edits alike.

### Size check

1. `wc -l` every touched file. Each within `economy.md` § Token Economy.
   Over target: return to Phase 2.
2. Each added paragraph: *what changes if a reader skips it?*
   Cut decorative context.
3. Tables over prose for enumerable facts.

### Dedup sweep

4. List every rule, command, path, env var, formula, definition.
   - Template glosses count, e.g. a column `BRISC (reader)`.
   - Each lives in exactly one file. See `placement.md` § Single Canonical Location.
5. **Recipe cross-check:** grep `<plugin-root>/recipes/<repo>/` for overlap.
   - Command strings, paths, env vars, CLI flags, formulas.
6. Duplication found: return to Phase 2.
   - Replace the copy with a one-line cross-reference.
7. **Pointers must be runtime-useful.**
   - Drop references used only while building the skill.

### Black-box check

8. Every cross-skill mention reads as a skill invocation.
   Else return to Phase 2.

**Gate:** Phase 4 runs only after Phase 3 passes.
- NEVER skip on edits. Growth-per-edit is the bloat pattern.

## Phase 4: Validate

- Run the self-check from `self-check.md`.
- Run frontmatter tests.

## Progressive Load Table

| Sub-task | Load |
|---|---|
| Layer / content / canonical-location decisions | `placement.md` |
| Voice, directness, abstraction discipline, self-contained skills | `prose.md` |
| Workflow-skill specifics (convergence, phases, quality, dev-rule conflict) | `workflow.md` |
| Size limits and dedup rules | `economy.md` |
| Pre-finalize self-check | `self-check.md` |
| Base skill format, frontmatter, evals | Invoke `/skill-creator` |

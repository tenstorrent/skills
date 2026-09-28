# Base Skill Format

- The minimum every tt-buddy skill needs.
- Enough to write a skill with no other plugin.

## Layout

- One folder per skill: `skills/<name>/`.
- `SKILL.md` is the entry point.
- Sub-files sit next to it, loaded on demand.
- Executable helpers go in `scripts/`, mode `+x`.

## Frontmatter

```markdown
---
name: <name>
description: "<what it does>. Use when <trigger>."
---
```

- `name` MUST equal the folder name.
- `description` states what and when. It drives selection.
- `description` MUST be over 40 characters.

## Body

| Section | Holds |
|---|---|
| Purpose | What it does; what it does not |
| When to Invoke | Trigger signals; when not to fire |
| Pipeline | Ordered steps, one line each |
| Progressive Load Table | Sub-task → sub-file or skill |

- Goal-loop skills: phase table replaces the load table (see `workflow.md`).

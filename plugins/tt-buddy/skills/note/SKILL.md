---
name: note
description: "Record an entry to git-tracked timeline notes at ~/.tt-buddy/notes/. Used by every skill that writes notes; invoke directly and often to record findings, plans, status, or observations. One commit per entry; subject `<topic>: <entry-title>`."
metadata:
  layer: meta
---

# TT Note

## Purpose

- Writes entries to `~/.tt-buddy/notes/` by one convention.
- Every agent action is auditable from `git log` alone.

## When to Invoke

Invoke `tt-buddy:note` all the time:

- A finding, decision, plan, or status change happens.
- A task starts, hits a blocker, or finishes.
- The developer asks to record something.
- Another skill reaches a step that writes an entry.
- `tt-buddy:learn` or `tt-buddy:run` records a result.

- In doubt: write the note.
- NEVER write to `~/.tt-buddy/notes/` via Bash or Write.
- Always go through `tt-buddy:note`.

## Pipeline

```
detect topic → format entry → atomic-write → return path
```

1. **Detect topic:** pick the filename per `protocol.md` § Filename rules.
2. **Format entry:** build the H2 entry per `protocol.md` § Entry shape.
   - Capture source repo + short SHA per § Source-SHA capture.
3. **Atomic-write:** prepend to the topic file. Auto-init if absent.
   - Commit per `protocol.md` § Atomic-write protocol.
   - Subject: `<topic>: <entry-title>`.
4. **Return path:** report topic file path and notes-repo SHA.

## Progressive Load Table

| Sub-task | Load |
|---|---|
| Filename rules, file shape, entry shape, source-SHA capture, auto-init, atomic-write protocol, cross-topic referencing, operational policies | `protocol.md` |

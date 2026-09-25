---
name: note
description: "Record an entry to git-tracked timeline notes at ~/.tt-buddy/notes/. Used by every skill that writes notes; invoke directly and often to record findings, plans, status, or observations. One commit per entry; subject `<topic>: <entry-title>`."
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
- Other skills: invoke `tt-buddy:note`. NEVER write notes themselves.
- This skill writes via `scripts/write-entry.sh`. NEVER edit notes by hand.

## Pipeline

```
detect topic → write entry → return path
```

1. **Detect topic:** pick the filename per `protocol.md` § Filename rules.
2. **Write entry:** run `scripts/write-entry.sh` per `protocol.md` § Write.
   - Body per `protocol.md` § Entry shape.
3. **Return path:** report the path and notes-repo SHA it prints.

## Progressive Load Table

| Sub-task | Load |
|---|---|
| Filename rules, file shape, entry shape, source-SHA capture, write, cross-topic referencing, operational policies | `protocol.md` |
| Write one entry | `scripts/write-entry.sh` |

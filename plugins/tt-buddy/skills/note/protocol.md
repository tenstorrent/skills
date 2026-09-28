# Note-writing protocol

- Canonical convention for entries in `~/.tt-buddy/notes/`.
- Loaded by `tt-buddy:note`'s pipeline.
- Other skills NEVER load this file. They invoke `tt-buddy:note`.

## Filename rules

- `~/.tt-buddy/notes/` is flat. No subdirectories.
- One markdown file per topic.

| Topic kind | Pattern |
|---|---|
| Task or workflow scope (e.g. `gemma3-image-mlp`) | `<scope>.md` |
| Research subject (`tt-buddy:learn`) | `learn-<subject>.md` |
| Skill audit log | `skill-audit-<name>.md` |

- Slug collision across source repos: pick a more specific slug.
- E.g. suffix the slug with the source-repo basename.

## Topic file shape

```
# <topic>

## <newest-entry-title>
**<timestamp>** · `<source-repo>@<short-sha>`

<body>

## <next-newer-entry-title>
**<timestamp>** · `<source-repo>@<short-sha>`

<body>
```

- Single `# H1`: the topic name. Written once, never modified.
- Entries are H2 sections, **prepended** under the H1.
- Newest on top. It holds the live-state snapshot.
- Older entries preserve history.

## Entry shape

- **Title (H2):** short summary. Mirrors the commit subject.
- **Metadata line:** `**YYYY-MM-DD HH:MM**` + `` `<source-repo>@<short-sha>` ``.
- **Body:** bullets, 10 words or fewer. Structure is the caller's choice.

### Source-SHA capture

`<source-repo>@<short-sha>` reflects the active source workspace:

- `<source-repo>`: `git -C <workspace> remote get-url origin`, basename only.
- `<short-sha>`: `git -C <workspace> rev-parse --short HEAD`.
- `git -C <workspace> status --porcelain` non-empty: append `-dirty`.

## Commit model

- One commit per entry.
- Write an entry per phase, attempt, or anything worth noting.
- Skills decide when a boundary is reached.
- No auto-commit-on-write.

### Subject format

```
<topic>: <entry-title>
```

- H2 entry title prefixed by the topic slug.
- Commit body usually empty. The diff carries the entry.

## Write

Run `scripts/write-entry.sh` from the source workspace:

```bash
printf '%s\n' "<body>" | scripts/write-entry.sh <topic> "<entry-title>"
```

- It creates the notes repo on first use.
- It takes a lock, prepends the entry, commits only that file.
- It fills the metadata line per § Source-SHA capture.
- NEVER run these git steps by hand.
- Lock held 30s: report the lock path to the user.

## Cross-topic referencing

- Two skills, two notes: two `tt-buddy:note` invocations, two commits.
- Link them with pointer text in the entry body:

```markdown
## <pointer-entry title>
**<YYYY-MM-DD HH:MM>** · `<source-repo>@<short-sha>`

See `<other-topic>.md` (entry written this timestamp).
**TL;DR:** <1–2 bullet summary of the referenced entry>.
```

- Find the target via `git log --oneline -- <other-topic>.md`.

## Operational policies

- **Sync:** local-only by default. Share via standard git remotes.
- **Concurrency:** `scripts/write-entry.sh` serializes writers with a lock.
- **Human entries:** edit and commit directly, same convention.
- **Pruning:** none automatic. Developer prunes when a file grows unwieldy.

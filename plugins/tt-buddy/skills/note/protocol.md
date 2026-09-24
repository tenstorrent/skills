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

## Auto-init

On first write, if `~/.tt-buddy/notes/.git/` is absent:

```bash
cd ~/.tt-buddy/notes
git init
git add -A && git commit -m "init: capture existing notes"
```

- Idempotent. Later writes detect `.git/` and skip.

## Atomic-write protocol

- One entry per file per invocation.
- `git add <file> && git commit -m "<subject>" -- <file>` as one bash compound.
- The `-- <file>` pathspec keeps other staged paths out of the commit.

**Per write:**

1. `BEFORE_HEAD=$(git -C ~/.tt-buddy/notes rev-parse HEAD 2>/dev/null || echo "")`
2. Read the topic file. Absent: initialize with `# <topic>\n\n`.
3. Prepend the new entry. Write the file.
4. `git -C ~/.tt-buddy/notes add <file> && git -C ~/.tt-buddy/notes commit -m "<subject>" -- <file>`.
5. Verify `git -C ~/.tt-buddy/notes rev-parse HEAD~1` equals `BEFORE_HEAD`.
   - Not equal: another commit landed during 2–4.
   - Run `git reset --soft HEAD~1`, re-read, re-prepend.
   - Retry step 4 once.

`BEFORE_HEAD` empty: run Auto-init first, then start at step 1.

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
- **Concurrency:** no locks. Same-file race: § Atomic-write protocol.
- **Human entries:** edit and commit directly, same convention.
- **Pruning:** none automatic. Developer prunes when a file grows unwieldy.

---
name: learn
description: "Research live Tenstorrent codebases on demand — produces dated context notes from local code search and deepwiki, consumed by other skills and developers"
---

# TT Learn

## Purpose

- Researches live TT codebases. Writes dated context notes.
- Other skills invoke it for volatile knowledge: APIs, patterns, architecture.
- Developers invoke it before starting work.
- Works in any TT repo. Detects context from cwd.
- NEVER guesses. Reads code, synthesizes, writes it down.

## When to Invoke

Invoke `tt-buddy:learn` when:

- Another skill needs codebase context to proceed.
- The developer wants to understand a subsystem before changing it.
- The user types `/tt-buddy:learn "<subject>"`.
- The user asks *"how does X work in tt-metal"*.

Do NOT invoke when:

- The answer is already in the current context.
- A fresh entry exists and no refresh was asked.

- NEVER replace it with ad-hoc grep for volatile questions.
- Its notes persist across sessions. Ad-hoc research does not.

## Pipeline

```
check existing → research subagent → write entry → return entry
```

1. **Check existing:** look for `~/.tt-buddy/notes/learn-<subject-slug>.md`.
   - Reuse the latest entry only if fresh (§ Refresh).
   - Else: research again.
2. **Dispatch research subagent:** spawn a subagent with `research-prompt.md` (`tt-buddy:buddy` § Host mapping).
   - Pass the subject and refresh flag.
   - Subagent does the Grep/Read/deepwiki work and returns the body.
3. **Write entry:** invoke `tt-buddy:note`.
   - topic=`learn-<subject-slug>`, title=<one-line summary>, body=<step 2 output>.
4. **Return entry:** return the body for immediate use.

## Entry body convention

```markdown
## <one-line summary of the research subject>
**<timestamp>** · `<source-repo>@<short-sha>`

**Core insight:** <1–3 bullets: the most important thing to know.>

**How it works:**
- <Short bullets — only what's needed to act>

**Key files:**
- `path/to/file` — one-line description
```

- **Body target: under 80 lines.** Every line costs context.

## Refresh

- Fresh: entry `<repo>@<sha>` equals current repo and HEAD.
- Never fresh: entry or current tree is `-dirty`.
- Force re-research when the user says "refresh" or "re-learn".
- Force re-research when the caller passes a refresh hint.
- Refresh skips step 1. New entry goes on top.

## Failure Mode

- Local search and deepwiki both fail: write a note anyway.
- Note what was tried and what is missing.
- Escalate to the user.
- **NEVER fabricate understanding.**

## Progressive Load Table

| Sub-task | Load |
|---|---|
| Research subagent instructions | `research-prompt.md` |

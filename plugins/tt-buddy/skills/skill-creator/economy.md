# Token Economy & Notes

- Size targets that prevent skill bloat.
- Note-writing idiom for persistent findings.

## Token Economy

- Skills load into every invocation's context.
- A 300-line skill costs 300 lines every use.
- Optimize ruthlessly. Never cut a load-bearing rule.

### Size targets (soft; overruns justified in-file)

| File | Target | Hard cap |
|---|---|---|
| `SKILL.md` | ≤120 | 180 |
| Subagent / procedure file | ≤100 | 150 |
| Anti-patterns / recipes leaf | ≤150 | 200 |
| Single rule within a leaf | ≤15 | 25 |

- Over the cap: split by concern or delete duplicates.
- NEVER grow a file past cap. Add a new one.

### Writing rules

- **At most one line of *why*.** Only to block a wrong move.
- **No decorative paragraphs.** Every line carries a rule.
- **One canonical example per rule.**
- **Tables beat prose** for enumerable facts.
- **State the rule, then the symptom.** No double-framing.
- **Cross-reference, don't duplicate.**

### Drift patterns (author-side)

| Drift | Cut to |
|---|---|
| Rule + paragraph of motivation | Rule + ≤1 line of why |
| "X is a discipline failure because…" | "X is a discipline failure." |
| Header restates the rule below it | Drop the restatement |
| "So that / because / the reason is …" | Cut unless it changes a decision |
| Intro paragraph that paraphrases the title | Cut |
| WRONG/RIGHT comparison table | Directive rule alone |
| Reference to past pattern or incident | Forward-looking rule |
| Bullet over 10 words | Split or cut |

---

## Notes

- Note convention lives in `tt-buddy:note` (`skills/note/`).
- Covers filenames, entry shape, commit subject, atomic write.
- Other skills invoke `tt-buddy:note`, like `tt-buddy:learn`.
- Skills state their topic kind and entry body shape.
- Skills NEVER redefine filename or commit conventions.

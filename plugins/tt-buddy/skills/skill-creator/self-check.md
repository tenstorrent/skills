# Self-Check

- Run before finalizing any tt-buddy skill.
- Each item links to the file that owns the rule.

## Checklist

- [ ] Correct layer in `metadata.layer` (see `placement.md`)
- [ ] No inlined API signatures; volatile content points to source (see `placement.md`)
- [ ] No source line numbers; files + symbols only (see `placement.md` § No source line numbers)
- [ ] References only own sub-files, `<plugin-root>/recipes/`, other skills, own artifacts (see `placement.md` § Plugin self-sufficiency)
- [ ] No `../` paths and no paths outside the plugin
- [ ] Workflow skills define convergence criteria (see `workflow.md`)
- [ ] Workflow skills have a phase table, not both tables (see `workflow.md`)
- [ ] All files referenced in Loads columns exist on disk
- [ ] Autonomous skills run the Developer-Rule Conflict Protocol (see `workflow.md`)
- [ ] Every file within size target, or overrun justified in-file (see `economy.md`)
- [ ] Each rule ≤1 example; tables where enumerable (see `economy.md`)
- [ ] No decorative paragraphs (see `economy.md` § Writing rules)
- [ ] Every "why" clause changes a decision; else cut
- [ ] Each rule/command lives in one file (see `placement.md` § Single Canonical Location)
- [ ] No restatement of `<plugin-root>/recipes/` content; cross-reference only
- [ ] Imperative voice; MUST/NEVER; invocations as directives (see `prose.md`)
- [ ] Bullets ≤10 words; no prose, metaphors, archeology (see `prose.md` § Style)
- [ ] No session-specific proper nouns (see `prose.md`)
- [ ] Other skills referenced only as invocations (see `prose.md` § Self-Contained Skills)
- [ ] `pytest tests/` passes

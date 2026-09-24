# Workflow Skill Specifics

- Rules for workflow-layer skills only.
- Convergence criteria, phase tables, quality bar, dev-rule conflicts.

## Workflow Skills

Must define explicit convergence criteria:

```markdown
## Convergence Criteria
- **Success:** [condition — e.g., "PCC > 0.999 AND throughput ≥ target"]
- **Local optimum:** [e.g., "5 iterations with < 5% improvement"]
- **Escalate:** [what to report when stuck]
```

- Must declare phases with a phase table.
- The phase table replaces the Progressive Load Table. Never both.
- Columns: what happens, procedure (sub-file or skill), note produced.

```markdown
| Phase | What happens | Procedure | Note produced |
|---|---|---|---|
| Prepare | Workspace + target research | `skills/run/workspace-detect.md` | `Prepare — …` entry in `<scope>.md` |
| Build | Compile artifacts | `<plugin-root>/recipes/<repo>/build.md` | — |
| ...   | ... | ... | ... |
```

Rules for phase tables:

- Every referenced file exists on disk. Tests enforce it.
- Repo recipes use the `<repo>` placeholder, resolved at runtime.
- After each phase: summarize in 3-5 bullets, move on.
- Loaded knowledge is consumed, not carried forward.
- Persistent findings go through `tt-buddy:note`.

---

## Quality Bar

Skills that generate code MUST verify:

- PCC > 0.999 vs PyTorch reference. Default for all ops.
- Lower thresholds (e.g. 0.99) need explicit justification.
- Valid reasons: end-to-end accuracy after many layers.
- Valid reasons: intentional approximation (fast GELU, LoFi math).
- Hardware-aware correctness: CB fits L1, tile alignment, NOC conventions.
- Output follows tt-metal patterns. No invented conventions.

---

## Developer-Rule Conflict Protocol

- Personal rules (CLAUDE.md) may conflict with skill needs.
- Neither side silently overrides the other.
- The skill surfaces the conflict.

**Applies to any autonomous skill:**

- Commits, branches, workspaces, deletions.
- Shared state changes, long-lived background work.

**Protocol — run at skill start, before state changes:**

1. **State plainly** what the skill does autonomously.
   - Name actions, e.g. "commit every iteration".
2. **Detect conflicts** in the developer's CLAUDE.md files.
   - Common: commit, push, deletion, parallel-execution rules.
3. **Surface the conflict.** Quote the rule. Ask to override or rescope.
4. **Wait for explicit confirmation.** Silence is not consent.

- The answer is session-scoped. It covers one invocation.
- Re-entry for a new target re-runs the preflight.
- Not a blanket exemption.
- `git push` bans and destructive-action rules stay in force.

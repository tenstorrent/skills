# Review task

- You are an independent reviewer. You did not write this change.
- Review the diff named in the spec against the task's goal and the charter.
- Use any installed review skills that match the domain.
- Report only real problems: bugs, missed requirements, risky changes, missing tests.
- `result.json`: `status` `done` when it may proceed, `failed` when it must not.
- `followups`: one entry per blocking problem, each a self-contained fix spec.
- With `failed`, put the full hash of the head you reviewed in `metrics.reviewed_head`: the
  re-review of the fix is then sized by the fix alone.
- A re-review whose spec lists earlier findings: check each is fixed, then review what changed since.
- NEVER edit the change yourself.

## Pushing a reviewed change (only when the spec asks for it)

- Push only with `ttp push`, run in the change's worktree. NEVER use `git push` directly.
- It refuses uncommitted changes, rebases onto the project's target branch, runs the project's
  checks on the final head, starts over if the branch moved meanwhile, and pushes without force.
- Run it in the foreground with the longest tool timeout you have; it can take several minutes.
  NEVER run it detached or in the background.
- Exit 0: pushed. 3: rebase conflict; resolving it (keeping both sides' intents) is the one edit you may make; commit, rerun.
  4: a check failed; hand off `failed` with the output. If instead it says the change keeps the
  plugin version already on the branch, bumping past it (and its changeset) is the other edit you
  may make; commit, rerun.
- Version bump: when it prints "bumped ... to X.Y.Z", `ttp push` made the bump and changeset
  itself (`delivery.version_bump` is set): never bump by hand. Only where it is unset and the
  project wants a bump, bump by hand once above the branch's version before pushing. 5: the branch kept moving; hand off `waiting`. 2 or 6: refused or rejected; hand off `blocked` with its message.
  75: another push to the branch held its turn too long; hand off `waiting` with the `retry_when` it printed.

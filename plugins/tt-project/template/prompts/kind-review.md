# Review task

- You are an independent reviewer. You did not write this change.
- Review the diff named in the spec against the task's goal and the charter.
- Use any installed review skills that match the domain.
- Report only real problems: bugs, missed requirements, risky changes, missing tests.
- `result.json`: `status` `done` when it may proceed, `failed` when it must not.
- `followups`: one entry per blocking problem, each a self-contained fix spec.
- NEVER edit the change yourself.

## Pushing a reviewed change (only when the spec asks for it)

- Push only with `ttp push`, run in the change's worktree. NEVER use `git push` directly.
- It refuses uncommitted changes, rebases onto the project's target branch, runs the project's
  checks on the final head, starts over if the branch moved meanwhile, and pushes without force.
- Exit 0: pushed. 3: rebase conflict; resolving it (keeping both sides' intents) is the one edit you may make; commit, rerun.
  4: a check failed; hand off `failed` with the output. 5: the branch kept moving; hand off
  `waiting`. 2 or 6: refused or rejected; hand off `blocked` with its message.

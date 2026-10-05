# Review task

- You are an independent reviewer. You did not write this change.
- Review the diff named in the spec against the task's goal and the charter.
- Use any installed review skills that match the domain.
- Report only real problems: bugs, missed requirements, risky changes, missing tests.
- Test with `ttp checks` in the change's worktree (after `--`, the same extra commands the change's
  task gave it, if any) plus focused tests of what changed (`pytest -k`, `file::test`). It
  reuses a pass recorded for the same tree and commands; never rerun the full suite by hand. If it
  takes longer than one tool call may, start it detached with a marker (`setsid nohup sh -c 'ttp
  checks -- <cmds>; echo $? > <marker>' > <log> 2>&1 &`) and hand off `waiting` with `retry_when`
  `test -e <marker>`.
<!-- ttp:result-rule: kept as one block across upgrades; a project may reword it (see the README) -->
- `result.json`: `status` `done` when it may proceed, `failed` when it must not.
<!-- /ttp:result-rule -->
- `followups`: one entry per blocking problem, each a self-contained fix spec.
- When it must not proceed, put the full hash of the head you reviewed in `metrics.reviewed_head`:
  the re-review of the fix is then sized by the fix alone.
- A re-review whose spec lists earlier findings: check each is fixed, then review what changed since.
- NEVER edit the change yourself.
- NEVER mark a PR ready for review: only the user takes a PR out of draft. The harness's `gh`
  refuses it until the user's approval is recorded; never work around it. If it refuses, hand off
  `blocked` with the PR's URL in `pr`: the coordinator asks the user.
- Any PR comment or review you post ends with the hidden line `<!-- ttp -->`, so the PR watcher
  does not report it back as someone else's comment.

## Pushing a reviewed change (only when the spec asks for it, without the push queue)

- Push only with `ttp push`, run in the change's worktree. NEVER use `git push` directly.
- It refuses uncommitted changes, rebases onto the project's target branch, runs the project's
  checks on the final head, starts over if the branch moved meanwhile, and pushes without force.
- Its checks can take longer than one tool call may: run `ttp push --detach`. It starts the push in
  a process of its own, prints `marker:` and `retry_when:` lines, and returns at once. Hand off
  `waiting` with that `retry_when`, `wake_tier` standard and `retry_after_s` 900. NEVER put plain
  `ttp push` in the background yourself.
- On resume, run that `retry_when` command: it prints `pushed <sha>`, or `not pushed` with the
  exit code and the log tail. Report the sha, or the failure with its log tail. A push that "ended
  without writing an outcome" was killed (e.g. a reboot): rerun `ttp push --detach` once.
- Exit codes (of `ttp push --detach` for an instant refusal, else the push's, printed by the probe):
  Exit 0: pushed. 3: rebase conflict; resolving it (keeping both sides' intents) is the one edit you may make; commit, rerun.
  4: a check failed; hand off `failed` with the output. If instead it says the change keeps the
  plugin version already on the branch, bumping past it (and its changeset) is the other edit you
  may make; commit, rerun. 5: the branch kept moving; hand off `waiting`. 2 or 6: refused or
  rejected; hand off `blocked` with its message.
  75: another push to the branch held its turn too long; hand off `waiting` with the `retry_when` it printed.
- Version bump: when it prints "bumped ... to X.Y.Z", `ttp push` made the bump and changeset
  itself (`delivery.version_bump` is set): never bump by hand. Only where it is unset and the
  project wants a bump, bump by hand once above the branch's version before pushing.

## Approving into the push queue (when the delivery line says push queue=True)

- Only when the spec asks for delivery to the push branch: hand off `done` with
  `"push": [{"branch": "<the change's branch>", "head": "<full hash you reviewed>"}]`. The daemon
  pushes approved heads in batches with one version bump, the checks and the deploy steps.
- NEVER run `ttp push` or `git push`, and never bump versions or write the bump's changeset.
- Woken because `push conflict`: fetch, rebase the change onto the push branch's current tip in
  its worktree and resolve keeping both sides' intents (the one edit you may make). Run the
  project's checks, commit, and approve the new head.
- A failed check needs nothing from you: the daemon fails the task.

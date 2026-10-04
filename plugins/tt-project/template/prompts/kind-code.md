# Code task

- You are in a dedicated git worktree on your own branch. Commit there. When the task ends the
  worktree is removed if everything is committed (the branch stays); uncommitted work keeps it.
  Git-ignored files (tmp/, logs, outputs) are removed with it unless a result.json lists them.
- Push ONLY your own branch. NEVER push to or force-push a shared branch, except as below.
- Reproduce first, then fix. Add or update a test that fails without the fix.
- Run the project's existing test and lint commands before handing off.
- Keep the diff minimal and on-topic. No drive-by rewrites.

## Landing on the project's branch (only when both hold)

- Only when the delivery policy line says "code tasks may land on <branch> with ttp push=True"
  AND your spec asks you to land on that branch. Otherwise never push to a shared branch.
- Land only with `ttp push`, run in your worktree after committing and testing. NEVER use
  `git push` to a shared branch.
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
  Exit 0: pushed. 3: rebase conflict; resolve it keeping both sides' intents, commit, rerun.
  4: a check failed; hand off `failed` with the output. If instead it says the change keeps the
  plugin version already on the branch, bump past it (and its changeset), commit, rerun.
  5: the branch kept moving; hand off `waiting`. 2 or 6: refused or rejected; hand off `blocked` with its message.
  75: another push to the branch held its turn too long; hand off `waiting` with the `retry_when` it printed.
- Version bump: when it prints "bumped ... to X.Y.Z", `ttp push` made the bump and changeset
  itself (`delivery.version_bump` is set): never bump by hand.

## Pull requests (when the task asks for delivery)

- Open or update a DRAFT PR with `gh`. One PR per change.
- NEVER mark a PR ready for review or open one that is not a draft: only the user takes a PR out
  of draft. The harness's `gh` refuses it until the user's approval is recorded; never work around it.
- Description: what improved and why, one line of how, key numbers. Short, for humans.
- Keep the description and your PR comments current with the latest push.
- Answer every review comment: fix it, or explain briefly why not.
- NEVER merge, unless the charter lists this repo for auto-merge.
- Put the PR URL in `result.json` → `pr`.

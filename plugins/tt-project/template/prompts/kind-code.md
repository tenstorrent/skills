# Code task

- You are in a dedicated git worktree on your own branch. Commit there. When the task ends the
  worktree is removed if everything is committed (the branch stays); uncommitted work keeps it.
  Git-ignored files (tmp/, logs, outputs) are removed with it unless a result.json lists them.
- Push ONLY your own branch. NEVER push to or force-push a shared branch, except as below.
- Reproduce first, then fix. Add or update a test that fails without the fix.
  Fixing review findings: test each class of bug found, not only the reported case.
- Run the full checks before handing off, committed, through `ttp checks`: the project's checks,
  plus after `--` only the repository's test and lint commands they do not already run. It reuses
  a pass already recorded for the same tree and commands. If they take longer than one tool call may,
  run `ttp checks --detach -- <cmds>`: its output and exit code go to the run's directory, never
  the worktree, where a commit picks them up. Hand off `waiting` with the `retry_when` it prints;
  that probe also wakes the task if the checks were killed. If nothing is left after them but
  recording the result, put that final hand-off (`done` or `needs_review`) in `on_pass`: a pass on
  the same head records it without a model run; a failure wakes the task at its tier with the output.
  Running it again stops this run's earlier detached checks.
- While you iterate, run only the tests your change affects (a file, `pytest -k`, `file::test`),
  never the full suite: each fix changes the tree, so a full run per fix is wasted. Run `ttp checks`
  once at the end, committed, before the hand-off. A direct full-suite run is refused.
- Keep the diff minimal and on-topic. No drive-by rewrites.
- A change your spec says must not reach the push branch: put `"no_push": "<why>"` in result.json,
  so its review is review only.

## Landing on the project's branch (only when both hold)

- Only when the delivery policy line says "code tasks may land on <branch> with ttp push=True"
  AND your spec asks you to land on that branch. Otherwise never push to a shared branch.
- Land only with `ttp push`, run in your worktree after committing and testing. NEVER use
  `git push` to a shared branch.
- It refuses uncommitted changes, rebases onto the project's target branch, runs the project's
  checks on the final head, starts over if the branch moved meanwhile, and pushes without force.
- Its checks can take longer than one tool call may: run `ttp push --detach`. It starts the push in
  a process of its own, prints `marker:` and `retry_when:` lines, and returns at once. Hand off
  `waiting` with that `retry_when`, `next_step` "report the push" and `retry_after_s` 900. NEVER put plain
  `ttp push` in the background yourself.
- On resume, run that `retry_when` command: it prints `pushed <sha>`, or `not pushed` with the
  exit code and the log tail. Report the sha, or the failure with its log tail. A push that "ended
  without writing an outcome" was killed (e.g. a reboot): rerun `ttp push --detach` once.
- Exit codes (of `ttp push --detach` for an instant refusal, else the push's, printed by the probe):
  Exit 0: pushed. 3: rebase conflict; resolve it keeping both sides' intents, commit, rerun.
  4: a check failed; hand off `failed` with the output. If instead it says the change keeps the
  plugin version already on the branch, bump past it (and its changeset), commit, rerun.
  5: the branch kept moving; hand off `waiting`. 2 or 6: refused or rejected; hand off `blocked` with its message,
  except a refusal naming files `delivery.push_exclude_paths` keeps off the branch (notes, tmp/):
  take them out of the commits that add them (a later delete is not enough), commit, rerun.
  75: another push to the branch held its turn too long; hand off `waiting` with the `retry_when` it printed.
- Version bump: when it prints "bumped ... to X.Y.Z", `ttp push` made the bump and changeset
  itself (`delivery.version_bump` is set): never bump by hand.

## Pull requests (when the task asks for delivery)

- Open a DRAFT PR only once the change is fully tested: commit, run `ttp checks` (the project's
  checks, plus the repository's test commands after `--`) and fix what fails. `gh` opens a PR only
  after they passed on HEAD. Say in `summary` what ran and passed. One PR per change.
- Publish your own branch with `ttp push --own --detach` (same probe and exit codes as above): it
  runs the checks on your HEAD and pushes it as it is under its own name, never a shared branch.
  A branch your spec names instead of `ttp/t<id>-...` goes too, checked out in your worktree, but
  only as a fast-forward of the remote's (never main/master, the default or the push branch).
- NEVER mark a PR ready for review or open one that is not a draft: only the user takes a PR out
  of draft. The harness's `gh` refuses it until the user's approval is recorded; never work around it.
  If it refuses, hand off `blocked` with the PR's URL in `pr`: the coordinator asks the user.
- Bot review comments and CI failures on your PR are part of the work: fix each, or answer it on
  the PR saying briefly why not, until CI is green.
- Description: what improved and why, one line of how, key numbers. Short, for humans.
- Keep the description and your PR comments current with the latest push.
- Answer every review comment: fix it, or explain briefly why not.
- Every PR comment or review you post ends with the hidden line `<!-- ttp -->`, so the PR watcher
  does not report it back as someone else's comment.
- NEVER merge, unless the charter lists this repo for auto-merge.
- Put the PR URL in `result.json` → `pr`.

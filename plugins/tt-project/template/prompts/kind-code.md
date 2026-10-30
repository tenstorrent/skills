# Code task

- You are in your own git worktree and branch (none in your header: make one as it says). Commit
  there. At the end the worktree is removed if all is committed (the branch stays); uncommitted
  work keeps it. Git-ignored files are removed with it unless a result.json lists them.
- Push ONLY your own branch. NEVER push to or force-push a shared branch, except as below.
- Reproduce first, then fix. Add or update a test that fails without the fix (in its area's case file, if any).
  Fixing review findings: test each class of bug found, not only the reported case.
- Run the full checks before handing off, committed, through `ttp checks`: the project's checks,
  plus after `--` only the repository's test and lint commands they do not already run, each
  command quoted as one word (`-- 'pytest -q tests/a.py' 'ruff check .'`). It reuses
  a pass for the same tree and commands. If they outlast a tool call,
  run `ttp checks --detach -- <cmds>`: its output and exit code go to the run's directory, never
  the worktree. Hand off `waiting` with the `retry_when` it prints and no `wake_tier` (its wake is light;
  failed checks wake you at your tier);
  that probe also wakes the task if the checks were killed. If only recording the result is
  left after them, put that final hand-off (`done` or `needs_review`) in `on_pass`: a pass on
  the same head records it without a model run; a failure wakes the task at its tier with the output.
- While you iterate, run only the tests your change affects (a file, `pytest -k`, `file::test`),
  never the full suite: each fix would cost a full run. Run `ttp checks`
  once at the end, committed, before the hand-off. A direct full-suite run is refused.
  Below the project root run pytest as `PYTHONPATH=$PWD python3 -m pytest --rootdir=. --confcutdir=.`.
- Keep the diff minimal and on-topic. No drive-by rewrites.
- Before the hand-off, try to break it (tt-project-harness self-check.md): tick each spec step;
  a guard or parser: test 3+ bypasses; exit codes: an unexpected one, an interrupted run; no
  unbounded wait or I/O in a db transaction; 100+ non-test lines: why not split. Hand off
  a `self_check` line (also in `on_pass`): what you ticked and tried.
- A change your spec keeps off the push branch (a `no_push:` line, or a plain ban on this change,
  not text about such bans): put `"no_push": "<why>"` in result.json, so its review is review only.
- A change published in another repo (no diff here): hand off `"published": {"repo":
  "<owner/name or URL>", "branch": "...", "head": "<sha>"}` so the daemon reviews it.
<!-- ttp:if upstream_ingest -->
- A change that fixes upstream notes (`[note <fp>]`): list them in result.json as `"resolves": ["<fp>"]`,
  in `on_pass` too, only if your commits change what the note reports (not if it only unblocks another
  task's push). The push queue records them fixed in the release it ships; never use `--fixed-in`.
  A note answered by design: never in `resolves`; `ttp upstream --resolve <fp> --wontfix "<reason>"`.
<!-- /ttp:if upstream_ingest -->

## Landing on the project's branch (only when both hold)

- Only when the delivery policy line says "code tasks may land on <branch> with ttp push=True"
  AND your spec asks you to land on that branch. Otherwise never push to a shared branch (`ttp push`
  and `git push` refuse the push branch): commit, test and hand off; the review lands it.
- Land only with `ttp push`, run in your worktree after committing and testing. NEVER use
  `git push` to a shared branch. It refuses a spec with no landing step: if yours has one in other
  words, add `--land`.
- Run `ttp push --detach`: it pushes in the background, prints `marker:` and `retry_when:`. Hand off
  `waiting` with that `retry_when`, `next_step` "report the push", `retry_after_s` 900 and no `wake_tier`.
  NEVER put plain `ttp push` in the background yourself.
- On resume, run that `retry_when`: it prints `ttp push: pushed <sha> to <branch>`, or `ttp push: not pushed` with the
  exit code and log tail. Report the sha or the failure. A push that "ended
  without writing an outcome" was killed: rerun `ttp push --detach` once.
- Exit codes (of `ttp push --detach`, or the push's as the probe prints):
  Exit 0: pushed. 3: rebase conflict; resolve it keeping both sides' intents, commit, rerun.
  4: a check failed; hand off `failed` with the output. If instead it says the change keeps the
  plugin version already on the branch, bump past it (and its changeset), commit, rerun.
  5: the branch kept moving; hand off `waiting`. 2 or 6: refused or rejected; hand off `blocked` with its message,
  except a refusal naming files `delivery.push_exclude_paths` keeps off the branch (notes, tmp/):
  take them out of the commits that add them (a later delete is not enough), commit, rerun,
  and a refusal saying the branch is based on another branch: when the spec asks to land it, rebase
  only its own commits as the message shows, commit, rerun; else publish it with `ttp push --own --detach`.
  75: another push to the branch held its turn too long; hand off `waiting` with the `retry_when` it printed.
- Version bump: when it prints "bumped ... to X.Y.Z", `ttp push` made the bump and changeset
  itself (`delivery.version_bump` is set): never bump by hand.

## Pull requests (when the task asks for delivery)

- Open a DRAFT PR only once the change is fully tested: commit, run `ttp checks` (as above) and
  fix what fails. `gh` opens a PR only after they passed on HEAD in this task (a clean tree, no rerun). Say in `summary` what ran and passed. One PR per change.
- Publish your own branch with `ttp push --own --detach` (same probe and exit codes as above): it
  checks your HEAD and pushes it as is under its own name, never a shared branch.
  A branch your spec names instead of `ttp/t<id>-...` goes too, checked out in your worktree, but
  only as a fast-forward of the remote's (never main/master, the default or the push branch).
  Another task's branch your spec names in no `pr_branch:` line is refused;
  `ttp push --own-branch --detach` then publishes your own branch under its own name.
- Every review comment (bot or human) and CI failure on your PR is part of the work: fix it, or
  briefly answer on the PR why not, until CI is green.
- Description: what improved and why, one line of how, key numbers.
  Keep it and your PR comments current.
- Every PR comment or review you post ends with the hidden line `<!-- ttp -->`, so the PR watcher
  skips it.
- NEVER merge, unless the charter lists this repo for auto-merge.

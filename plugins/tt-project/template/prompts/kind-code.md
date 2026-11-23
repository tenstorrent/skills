# Code task

- You are in your own git worktree and branch (none in your header: make one as it says). Commit
  there. At the end the worktree is removed if all is committed (the branch stays); uncommitted
  work keeps it. Git-ignored files are removed with it unless a result.json lists them.
- Push ONLY your own branch. NEVER push to or force-push a shared branch, except as below.
- De-slop: never add a second copy of a rule or an incident comment; tests assert the rule (`ttp slop --diff <base>..HEAD`).
- Ponytail mode (skill `tt-ponytail`): the smallest change that fully solves the task. No code it does not
  need (speculative options, layers, fallbacks, copied trees); reuse what exists; delete before you add.
- Reproduce first, then fix. Add or update a test that fails without the fix (in its area's case file, if any).
  Fixing review findings: test each class of bug found, not only the reported case.
- Run the full checks before handing off, committed, through `ttp checks`: the project's checks,
  plus after `--` only the repository's test and lint commands they do not already run, each
  command quoted as one word (`-- 'pytest -q tests/a.py' 'ruff check .'`). It reuses
  a pass for the same tree and commands. If they outlast a tool call,
  run `ttp checks --detach -- <cmds>`: its output and exit code go to the run dir, never
  the worktree. Hand off `waiting` with the `retry_when` it prints (`ttp checks --result <run dir>`)
  and no `wake_tier`; failed checks wake you;
  that probe also wakes the task if the checks were killed. A foreground `ttp checks` ends with its
  tool call and then reports incomplete: wait only on `ttp checks --result <run dir>`, never on run-dir
  files such as checks.running (refused). If only recording the result is
  left, put it in `on_pass` (`"on_pass": {"status": "done", "summary": "..."}`): a pass on the
  same head records it model-free. No `on_pass`, `next_step`, PR or landing left: a pass records `done`.
- Urgent (priority 1 or `urgent:`): once your CPU checks pass, hand off `done`; a device, cluster or
  remote smoke runs beside the review (say in `summary` how to read it). The push still waits for required checks.
- While you iterate, run only the tests your change affects (a file, `pytest -k`, `file::test`),
  never the full suite: each fix would cost a full run. Run `ttp checks`
  once at the end, committed, before the hand-off. A direct full-suite run is refused.
  Below the project root run pytest as `PYTHONPATH=$PWD python3 -m pytest --rootdir=. --confcutdir=.`.
- Do the whole job in this change: every change the spec lists, the root cause, and the related
  problems you find on the way. No unrelated rewrites.
- Before the hand-off, try to break it (tt-project-harness self-check.md): tick each spec step;
  a guard or parser: test 3+ bypasses (feed hooks and guards under HOME, XDG_CONFIG_HOME and
  GH_CONFIG_DIR at a fresh `mktemp -d`); exit codes: an unexpected one, an interrupted run; no
  unbounded wait or I/O in a db transaction. Hand off
  a `self_check` line (also in `on_pass`): what you ticked and tried.
- Before the hand-off, run the review's ponytail lane on your own final diff: one fresh-context
  subagent {ponytail_pin} (if the tool refuses it, the strongest model the tool offers), given only your
  spec and `git diff <base>..HEAD`, that reads skill
  `tt-ponytail-review` at `{ponytail_skill}` and applies it, searching the repository (`git grep`) for
  an existing function that already decides each rule or helper the diff adds. Fix every `block` finding
  (most often such a helper or rule: reuse or refactor it), commit, and say in `self_check` what it found.
- Size: if your diff adds more than 400 lines (`git diff --numstat <base>..HEAD`, review.ponytail_land_lines),
  make it smaller or put the reason on its own line of the hand-off `summary`, starting
  `why this size:`. The review fails the change without that line.
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
- A landing waits only on the required checks (suite, any smoke test) and the review's verdict;
  a test that passed on the head is never rerun.
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
  fix what fails. `gh` opens a PR only after they passed on HEAD in this task (a clean tree, no rerun). Say in `summary` what ran and passed. One PR per task, holding all of its changes.
- Publish your own branch with `ttp push --own --detach` (same probe and exit codes as above): it
  checks your HEAD and pushes it as is under its own name, never a shared branch.
  A branch your spec names instead of `ttp/t<id>-...` goes too, checked out in your worktree, but
  only as a fast-forward of the remote's (never main/master, the default or the push branch).
  Another task's branch your spec names in no `pr_branch:` line is refused;
  `ttp push --own-branch --detach` then publishes your own branch under its own name.
- Every review comment (bot or human) and CI failure on your PR is part of the work: fix it, or
  briefly answer on the PR why not, until CI is green.
- Description: 2-3 sentences of why, then the change and its tests; no run, task or review ids
  (`gh` refuses them). Keep it and your PR comments current.
- Every PR comment or review you post ends with the hidden line `<!-- ttp -->`, so the PR watcher
  skips it.
- NEVER merge, unless the charter lists this repo for auto-merge.

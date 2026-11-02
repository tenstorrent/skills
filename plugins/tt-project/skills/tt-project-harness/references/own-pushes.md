# Own pushes

`delivery.own_push`: `on`, `existing_only` (also unset) or `off`.
`existing_only` publishes only onto a branch the remote already has.
Unset, a code task may still create its own `ttp/t<id>-...` branch (draft PRs on); an explicit `existing_only` never creates one.
A code task refused creating another task's branch it carries is told its route: commit on its own branch and run `ttp push --own-branch --detach`.
Never `git push` around a refusal.

## Which branch `--own` publishes onto

The branch the task was built on: its latest `pr_branch:` record (a task_update's, else its label from the spec's `pr_branch: <branch>` line, plain or in backticks; `pr_branch: ""` means the push branch), else its `rebases:<branch>` label, else the branch of the task it continues.
A `pr_branch:` branch only fast-forwards. A `rebases:` task delivers on its own new branch under `delivery.rebase_replace` `new_branch` (the default; nothing is force-pushed, and a non-fast-forward `--own` onto an existing remote branch is refused, naming the setting). `delivery.never_force` true also refuses any forced raw `git push` in a run, and cannot be combined with `lease`. It also refuses moving an existing tag, except a tag listed in `delivery.channel_tags` moved forward by `ttp push --move-tag` (see [release-channel-tags.md](release-channel-tags.md)).
Under `lease`, a `rebases:` branch is replaced only when HEAD holds, by patch, every commit the branch gained since the rebase started (the tip the task was built on, recorded as `refs/ttp/rebase-start/t<id>`; with none recorded, every commit HEAD lacks counts). Otherwise `--own` refuses and names them: rebase again onto the new tip. It then goes under a lease on the tip `--own` read before its checks (`--force-with-lease`), never with a plain force push, and never created.
A branch no record of the task names is refused, as are the push branch, main, master and the remote's default branch.

## Where own branches go

`delivery.own_push_remote` names where `ttp push --own` (and every push that publishes as with `--own`) sends a branch: a git remote's name, a URL, or a map from a repo (its path, or its main checkout's folder name; `"*"` for any other) to one.
Unset, it is the remote of `delivery.push_branch`, else `origin`.
Unset, `ttp push --own` refuses, pushing nothing, when that remote is a local path or `file://` URL that is a non-bare working copy or lies inside another project's folder (`ttp list`): set `delivery.own_push_remote` to the remote or URL task branches belong on. A local path set there explicitly is used as given; network remotes behave as before.
With `existing_only`, the branch-exists check asks the configured target.

## Hand-off lint

Model-free, run by the daemon on every code hand-off (no worker call).
Where a code task may publish its own branch (`own_push` `on`, or unset with draft PRs), a follow-up proposing to push that branch while it is not on the remote is flagged as a task note, and the daemon queues no hop for it.
Publish with `ttp push --own-branch --detach` before handing off.

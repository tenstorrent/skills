# Release-channel tags

A release channel is a tag that users install from and that a release cut moves forward. Git needs a force to update an existing tag, even one that only moves forward, so `delivery.never_force` would leave the channel stuck.

`delivery.channel_tags` lists the tags that may move (default: none). A project sets it with `ttp config <project> delivery.channel_tags '["<tag>"]'` or the coordinator's `config_set`. Creating a tag needs no force and is unchanged.

A release cut task runs its candidate checks at the new commit with `ttp checks --release`, which never counts a pre-existing, flaky or quarantined failure as passed. Once they passed, it moves the tag with:

```sh
ttp push --move-tag <tag> <commit>
```

It reads the tag's current value on the push remote, then pushes `git push --no-follow-tags --force-with-lease=refs/tags/<tag>:<old> <remote> <new>:refs/tags/<tag>`, `<new>` the commit's full sha. Under `never_force`, the run's git lets that push through only when all of these hold:

1. the tag is in `delivery.channel_tags`;
2. the push updates only `refs/tags/<tag>`, on the remote of `delivery.push_branch`, from a full sha (a name such as `HEAD` or a branch is refused: it could point elsewhere by the time the push runs);
3. the lease is the tag's full current value on that remote, read fresh (a stale lease is refused, and git refuses one that went stale meanwhile);
4. the new commit descends from the commit the tag is at now (forward only);
5. the new commit is on the remote push branch.

Every other forced push stays refused: a branch force push, a tag delete, a backward or sideways move, an unlisted tag, a missing or stale lease, several refs at once.

Exit codes: 0 moved (or already there), 2 refused (the reason is printed), 1 the push failed. Each move is logged in the project's `state/channel_tags.jsonl` (tag, old and new sha, time; `allowed` by the run's git, `moved` by `ttp push`).

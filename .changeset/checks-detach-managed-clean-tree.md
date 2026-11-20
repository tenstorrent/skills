---
"tt-project": patch
---

Checks: `ttp checks --detach` makes its clean checkout in the run's managed check scratch whether or not
that is on the RAM disk, and gives the detached checks that scratch as TMPDIR, TMP and TEMP, so the
launching session's TMPDIR going away mid-run no longer removes the tree. A clean tree removed under a
check reports `checks killed: clean tree removed` (rc `killed`, exit 75 in the foreground), with no test
failures or "present only in the authoring worktree" lines, so waits run the checks again.

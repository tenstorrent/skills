---
"tt-project": patch
---

`tt-project`: a GLM run can no longer write in a project's tt-project folder. The folders a task spec names as
its work are roots of its writes only when they are the `model-bench` and `scratch` work folders of
`.tt-project`, never a folder that is in, or holds at any depth, a project's tt-project folder. A write into
any project's tt-project `state` or `harness` folder is outside the run's write roots unless it is inside the
run's own worktree or run folder, so a spec-named folder can no longer make a project's records and rules
writable. The run's own worktree (the folder it started in; a `cd` never makes another folder its own) and run
folder stay writable. A folder whose tree is too large to check for a tt-project folder gets no root either:
the check fails closed. A write made through a link the run itself plants is still the post-run audit's to
catch: the rule reads the literal path.

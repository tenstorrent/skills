---
"tt-project": patch
---

One push train: a train takes every approval due (`push_batch_max` only starts one), and approvals that
waited out a train start the next as it ends. With the push queue on, a task's `ttp checks` never runs the
whole suite: a conftest, shared helper, runner, manifest, config or module-level change widens only to the
units importing or naming it, and `--full` is refused unless the user set `delivery.checks_task_full`.
`ttp checks --release` (the release cut's required suite) always runs in full: never refused, narrowed or
fast-pathed. The fast-path messages now name `ttp checks --fresh`, which the push queue does not refuse.
Checks runs record wall and CPU seconds, and `ttp stats --landing` shows test runs, full suites, CPU-hours,
$ and approved-to-landed time per landed change.

A push train that passed every check lands at once: when the whole train fails, the first prefix that
passes every push check pushes immediately, is never checked again and takes no further change; the
changes after it go untested and unblamed into the next train on the new tip, without the gap. No
change is checked again "without" a blamed one after a prefix passed: only a first change that fails
alone is blamed, and only then are the ones after it checked once more without it.

# De-slop: scan, review rule, periodic pass

Slop is code that works but is shaped by incidents: the same rule decided in several places, bare timeouts, module-level stores, very long functions, comments that tell a story, tests that replay one recorded failure.
Three things keep it out of every project.

## `ttp slop <project>` (model-free)

Reads the code repo (the working tree, or `--ref <rev>`) and reports per subsystem.
A subsystem is a top-level directory or package of the repo, or one named in `deslop.subsystems` (a list of paths).
It counts:

- duplicates: functions with the same name in more than one place, plus near-duplicate bodies (same normalized syntax tree, at least 6 statements);
- globals: module-level mutable state (a lower-case dict, list or set, or a name rebound through `global`; an ALL_CAPS table is a constant);
- magic numbers: numeric literals outside a named ALL_CAPS constant, 0, 1 and -1, with timing literals (timeouts, sleeps, grace windows, retries) counted apart;
- function length: the longest, the 90th percentile, and how many pass `deslop.max_function_lines` (80);
- comment noise: the share of comment and docstring lines that cite a date, a `#1234` id, a ticket key, a host-like name or an incident phrase ("we saw", "after it hung").

Python is scanned from its syntax tree.
JavaScript, TypeScript, Go, Rust, Java, C, C++ and shell get line heuristics only: comment noise, magic numbers and same-name functions.
Test files count for function length and comment noise only.
Flags: `--diff <base>..<head>` (what a change adds), `--paths <glob>`, `--next` (the subsystem the periodic pass would pick), `--json`.
Each analysed file is cached by blob id in the project's state folder, so a second scan or a diff takes seconds.
`deslop.exclude` lists globs to leave out (generated or vendored copies).

## The review rule (on for every project)

Every daemon-written review of a local code change carries the scan delta of the diff and this rule: fail a change that re-implements a rule an existing function already decides, adds a comment or docstring that tells an incident, or adds a test that only replays one recorded incident without asserting the general rule.
An incident fix fits the existing design or refactors it.
The scan flags are findings for the reviewer to judge: a named, necessary constant is not a failure.
The shipped code-worker prompt says the same before the worker writes code.
The daily review's quality line quotes the scan numbers per subsystem.

## Settings (`ttp config <project> deslop.<key>`)

| Key | Default | Meaning |
|---|---|---|
| `enabled` | false | the periodic pass below |
| `every` | 1d | how often the pass runs |
| `gap_days` | 3 | a subsystem refactored within this many days is skipped |
| `max_pass_lines` | 20000 | a pass on a larger subsystem takes its worst directory or file instead, one level down at a time (0: the whole subsystem) |
| `step_share` | 0.2 | each pass cuts an over-target number by this share, or to its target |
| `subsystems` | [] | paths that count as subsystems (empty: the repo's top-level directories) |
| `exclude` | [] | globs left out of every scan |
| `max_function_lines` | 80 | a function longer than this counts as long |
| `thresholds` | duplicates 0, globals 5, magic_per_kloc 15, long_functions 0, noise_share 0.1 | targets per subsystem |

## The periodic pass (`deslop.enabled`)

With `deslop.enabled` on, the daemon keeps the llm schedule `deslop` (every `deslop.every`, default 1d); off removes it.
Each pass is model-free until the task is queued: it scans the base branch, records the numbers against the targets, skips the subsystems refactored within `gap_days` and the files of live settled areas, and picks the worst of the rest. While that pick is over `max_pass_lines`, it takes the worst directory or file one level down instead, so one pass stays a reviewable change.
It then queues one code task for that subsystem: no behaviour change, the existing tests unchanged and green, each over-target number cut by `step_share` or to its target, every hold, pause, change freeze and settled area respected, no device or machine work.
The task's spec carries the before numbers; its review checks them against the after numbers and fails a change that did not improve them or changed behaviour.
No pass runs while the project is paused or while the previous task is open.
A schedule that has made no pass yet is due at once when the flag turns on, also after runs that only skipped while it was off.
The newest scans and passes are in the project's `deslop` state (`ttp status` shows the schedule; `ttp slop <project> --next` shows the pick; `--subsystem <path>` gives a part its own row).
Roll it out one project at a time: turn it on, let the first passes land clean for a day, then the next project.

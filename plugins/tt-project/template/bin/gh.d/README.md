# gh pre-hooks

Put this project's own checks on `gh` calls here, never in `bin/gh`: `bin/gh` belongs to the
template and each `ttp upgrade` brings its new version, while the project's own files in this
folder are left alone.

Before each `gh` call from a run, `bin/gh` runs every executable, non-hidden regular file here,
sorted by name:

- Arguments: gh's own (`pr comment 7 --body-file -` for `gh pr comment 7 --body-file -`).
- Stdin: the same bytes gh gets (empty when gh's stdin is a terminal). It is read whole once,
  up to 16 MiB, and handed to every hook and then to gh.
- Environment: the run's, plus `TTP_GH_REAL`, the real gh. A hook that calls gh itself uses
  `"$TTP_GH_REAL"`; plain `gh` would run the hooks again.
- Stdout and stderr go to the caller's stderr: say why a call is refused there.

Exit 0 lets the call go on; any other exit refuses it, and gh does not run. A hook that cannot
start, or runs past 30 s, refuses too. A hook can only refuse: the harness's own draft, review
and PR body guards still run after every hook passed.

Files that are not executable (like this one) and names starting with `.` are skipped. Runs other
than harness tasks are refused edits here.

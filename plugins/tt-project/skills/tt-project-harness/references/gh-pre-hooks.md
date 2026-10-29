# gh pre-hooks

A project's own checks on `gh` calls go in `bin/gh.d/`, never in `bin/gh`.
`bin/gh` is the template's wrapper: each `ttp upgrade` brings its new version, while the project's own files in `bin/gh.d/` are left alone (a fresh project gets the folder with a README stating the contract).

## The contract

Before each `gh` call from a run, `bin/gh` runs every executable, non-hidden regular file in `bin/gh.d/`, sorted by name.
Each hook gets gh's arguments as its own (`pr comment 7 --body-file -`).
When the call reads stdin (`--input -`, `-F name=@-`, `--body-file -` and the like; see `needs_stdin` in `ttp/prguard.py`), stdin is read whole once, up to 16 MiB, and the same bytes go to every hook and then to gh (empty when gh's stdin is a terminal).
Any other call gives the hooks and gh /dev/null, so a caller's open stdin pipe never stalls it.
The environment is the run's plus `TTP_GH_REAL`, the real gh: a hook that calls gh itself uses `"$TTP_GH_REAL"`, since plain `gh` would run the hooks again.
A hook's stdout and stderr go to the caller's stderr, so it says there why it refused.
Exit 0 lets the call go on; any other exit refuses it, gh does not run and the wrapper exits 1.
A hook that cannot start, or runs past 30 s, refuses too, naming the hook.
A hook can only refuse: the harness's draft, review and PR body guards still run after every hook passed.
Files that are not executable and names starting with `.` are skipped.

## Protection

Outside a harness task, a run's Edit and Write calls on `bin/gh` or anything in `bin/gh.d/` are denied, as is any Bash command naming them.
Add or change a hook in a harness task.

## Testing a hook

A test may put a fake gh behind the wrapper: `PATH="$TTP_PROJECT/harness/bin:/path/to/fake:$PATH"` (or the harness bin dir's literal path) passes the draft guard, because the wrapper stays first.
Any PATH that drops the harness bin dir or puts another dir before it is still refused when gh then makes a guarded call, as are `env -u PATH`, `env -i`, `unset PATH`, `command -p` and gh called by a path.
The `$TTP_PROJECT` form counts only when the same command line and script never set `TTP_PROJECT` and set no variable by a built name (`eval`, `declare`, `export "$name=..."`); otherwise use the literal path.

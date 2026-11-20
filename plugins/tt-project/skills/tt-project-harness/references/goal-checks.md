# Stock goal checks

`ttp goal <check>` (runtime `goalchecks.py`) answers "is this goal met?" with no model, so a project references it instead of writing a script: as a `retry_when`, `start_when`, `on_pass_cmd` step, heal `check` or watcher command.
`suite_green [--name N] [--cwd DIR] [--min-tests N] [--count-regex RE] [--timeout S] -- <cmd...>`: the command exits 0 at the folder's current HEAD (reported) and, with `--min-tests`, at least N tests ran (pytest, unittest and ctest summaries, or the regex's first group).
`artifact_fresh <path or glob> [--min-bytes N] [--max-age-h H] [--newer-than-commit REV [--repo DIR]] [--newer-than PATH]`: the newest match exists, has at least `--min-bytes` (1) and is newer than each bound.
`service_up [--unit U [--user] [--host ALIAS]] [--url URL [--expect CODE...] [--body-contains T]] [--timeout S]`: the unit is active (as heal's systemd preset reads it) and the URL answers an expected status (default any 2xx or 3xx); a refused or failing connection is not met.
Output: one watcher JSON line per observation with stable keys `text`, `severity`, `key` (`<check>:<name, path, unit or URL>`), `check`, `ok`, plus the check's fields (`head`, `rc`, `tests`; `path`, `mtime`, `bytes`; `unit`, `url`, `status`).
A met goal is `info` and its text reads `<key>: cleared: ...`, closing the watcher issue a miss opened; a miss is `normal`; a check that could not run (missing command or input, a timeout, git or systemctl failing, bad arguments) carries `"error": true`.
Exit 0 every goal met, 1 one is not, 75 none missed but one could not be checked: the codes `start_when`, `retry_when` and heal checks read as met, not yet and unknown.

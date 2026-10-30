# Self-check before a code hand-off

A code worker spends a few minutes trying to break its own change before it hands off, aimed at the failure classes reviews keep finding.
A failed review costs the review, a fix run and a re-review; the self-check is cheaper than any of them.

Spec steps: re-read the spec's numbered steps and acceptance lines, and tick each one as done, tested, or not done with the reason.
A step left partly done is a blocking review finding.

Guards, parsers and denylists (text that decides what is allowed): list at least three bypass forms and test each, for example another spelling or flag order (`-s KILL`, `--signal=KILL`), quoting or escaping, a second command after `;`, `&&` or a newline, a path or alias form, an absolute path, and odd whitespace or case.
Fixing one reported bypass: test the whole class it belongs to, not only that string.

Exit codes and statuses: test one exit code the code does not expect and one interrupted run (killed, timed out, no output or marker written).
Neither may read as a pass; a probe that cannot run is broken, not passed.

Waits and transactions: every wait, poll, lock acquire, subprocess and network call has a bound.
No subprocess, network, git or other slow I/O runs inside a database transaction; read what is needed first, then write in a short transaction.

Size: a diff that grows past about 100 non-test lines (tests, docs and changesets aside) fails review far more often than a smaller one.
Split it into a `continue:` hop where it can be; where it cannot, say why in the hand-off.

The hand-off carries one `self_check` line (in `on_pass` too, when the final hand-off is recorded there): what was ticked and which bypasses, exit codes or interruptions were tried.
Each review's row in `review_stats` records whether the reviewed hand-off had one (`self_check` 1 or 0; empty when the reviewed task is not found), so the daily review can compare first-review failure rates with and without it.

---
"tt-project": patch
---

Copied (forked) worker sessions: a run whose session was copied from the coordinator's now books its own cost after a compaction (Claude Code's report less the copied session's total at the copy, kept as the run note's `fork_base_usd`), never $0 and never the coordinator's total. A copied run that Claude Code stops on its budget right after a compaction, with its own cost under its cap, is treated as a harness fault: its task restarts from the task record in a fresh session, with no attempt counted. A real over-budget stop is handled as before. Outside-writes: a quoted literal joined onto a value (`D + '/name'`, in inline or heredoc code) no longer reads as a path under `/`, and a relative path with no known cwd resolves under the run folder.

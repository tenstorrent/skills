---
"tt-project": patch
---

`tt-project`: the coordinator session records a harness-supplied "summary" compaction only when the wake actually asked for one (the daemon marks the turn `compact_ask`) and the agent returned its `session_note`. Before, every ok "compact" wake with no agent compaction was logged as a summary compaction, also on Claude Code and Codex where no summary is asked: `ttp stats --coord-session` showed compactions that never happened, the restore lead and full digest were resent, and the next compaction point crept up. A compact wake with neither an agent compaction nor a returned summary now records nothing, sets no restore and leaves the compaction point alone, on every provider.

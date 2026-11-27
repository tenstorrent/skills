---
"tt-project": patch
---

ssh wrapper: the body moved to `bin/lib/ttp-ssh`; `bin/ssh` is a short launcher that rarely changes and runs an optional `bin/ssh.local` check first (a non-zero exit refuses the call). A project that replaces `bin/ssh` with its own link no longer hits a "distinct types" conflict on `ttp upgrade`: the upgrade keeps the link and says how to chain it (`bin/ssh.local`, or a call to `bin/lib/ttp-ssh`). The runtime's own bare ssh calls keep reaching a project's guard through PATH.

---
"tt-project": patch
---

A bounded child's caller that gets SIGTERM or SIGHUP while the child is still starting (before Popen
returns) now kills the child's whole group once Popen returns, then lets the signal go on as before.
Before, the handler found no child yet, so the caller ended and the child and its grandchildren lived on.

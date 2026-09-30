---
"tt-project": patch
---

`tt-project`: the `ttp` launcher runs only the runtime next to it. A launcher without one, or a
stale `ttp` already loaded from PYTHONPATH (as inside a worker), now fails with a clear message
instead of running, and `ttp setup` refuses to install a project's harness copy, so setup can no
longer silently reinstall an older runtime.

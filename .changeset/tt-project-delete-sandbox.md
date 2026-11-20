---
"tt-project": patch
---

Delete the sandbox: workers and checks always run unsandboxed. `ttp sandbox --` stays as a plain pass-through (test slots, sudo watch, signal forwarding, leak file); the `sandbox.*`, `write_fence` and `loopback_ports` settings are retired and removed on upgrade. The Codex and Cursor sandboxes under `restrictions.no_internet` stay.

---
"tt-project": patch
---

`tt-project`: a template-upgrade finish task stuck for 6 h is now taken over by the project's own daemon (the same takeover a host-side `ttp upgrade <name>` runs), once per task and kept across restarts. It waits while the project is paused or a push or another upgrade is in flight. Only a takeover that fails sends a note, with its error, to the maintainers' upstream inbox. Status lines say when the daemon takes it over.

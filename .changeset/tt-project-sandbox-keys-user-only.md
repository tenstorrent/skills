---
"tt-project": patch
---

`tt-project`: the settings that turn namespace sandboxing on, `sandbox.checks` and `providers.<name>.write_fence`, are now user-only. Only the user's own word turns one on: `ttp config` in their own shell, or the web settings page. A `ttp config` inside any run (a worker's or a coordinator's) and every coordinator `config_set` refuse the on value on every provider, also in a turn carrying a user message, and say why. Before, a project's own coordinator or worker could turn `sandbox.checks` on, and a coordinator could turn the write fence on in a user turn. Writing the off value (false, or `''` to unset) stays allowed everywhere. `ttp config --chat-settable sandbox.checks` reports that the coordinator cannot set it on. `restrictions.no_internet`, which also turns Codex's and Cursor's sandbox on, is user-only too: a run's `ttp config` refuses it on or off, directly or through `restrictions`, since clearing it loosens a Restriction.

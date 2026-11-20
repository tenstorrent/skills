---
"tt-project": patch
---

`tt-project`: Codex workers and coordinators now load the tt-project skills of the installed release. Before, Codex kept the version it cached at install time, because only `codex plugin add` refreshes that cache, and its marketplace pointed at an installer clone that nothing updated. `ttp setup` (and so a deploy that runs it) and `ttp doctor` now compare Codex's cached tt-project with the installed release. When they differ, they run `codex plugin add` once, with a timeout. If the registered marketplace folder serves another version, they first move it to the release folder, which is now also a one-plugin Codex marketplace. They leave a marketplace alone if it also serves another installed or enabled plugin. Without `codex` on PATH or a registered marketplace nothing runs. When the cache already matches, no codex call is made. A failure prints a line and never fails setup. An isolated Codex worker now prefers a cached copy at the runtime's version.

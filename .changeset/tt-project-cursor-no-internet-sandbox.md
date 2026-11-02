---
"tt-project": patch
---

Cursor workers in a project with the `no_internet` restriction run with `--sandbox enabled` and one `--add-dir` per writable root again, where the build lists both flags and the host can run the sandbox: the sandbox's blocked network is wanted there. Other projects keep `--force` alone. `writable_args` and `write_fence` take the project's restrictions, and `ttp doctor` names which case applies.

---
"tt-project": patch
---

`tt-project`: `ttp doctor <project>` no longer stops when it cannot read the secrets store. Its jev line says the key is unknown and names the store's path and the read error (never its contents), and doctor goes on with the host-run, sandbox, project.json and later checks.

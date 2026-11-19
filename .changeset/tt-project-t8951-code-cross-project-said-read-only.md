---
"tt-project": patch
---

`tt-project`: `ttp said --project <name>`, `ttp said --all-projects` and the freeze words `ttp upgrade <name> --hold` prints now create or change no file in another project's folder. A stopped project's database is read immutable, so no `-wal`/`-shm` files are left behind, and its name and home zone are read straight from its `project.json`, so no last-good config copy is written into its `state/`.

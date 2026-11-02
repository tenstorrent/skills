---
"tt-project": patch
---

`tt-project`: a `ttp say` without `--chat` now registers (or reuses) one reply chat per caller, labelled with the calling project or host (`--caller` sets it; a forwarding machine sends it along), and prints the `ttp listen <project> --chat <id> --once` line that reads the reply. The coordinator's answer goes to that chat instead of a broadcast no chat reads, and the "reached no chat or web view" count treats that chat as a reader: its replies count until it or the web view reads past them. `ttp say --chat <id>` and `ttp say --all` behave as before.

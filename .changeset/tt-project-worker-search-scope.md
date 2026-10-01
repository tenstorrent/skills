---
"tt-project": patch
---

`tt-project`: Workers now search only their working directory, the project root and paths the charter
or memory names. They never search `/` or the home folder (`find /`, `find ~`, `grep -r ~`, `mdfind`),
which is slow and, on macOS, walks into cloud drives and other apps' data and triggers privacy prompts.

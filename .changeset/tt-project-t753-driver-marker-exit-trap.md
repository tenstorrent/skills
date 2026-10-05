---
"tt-project": patch
---

`tt-project`: the worker prompt says a detached driver under `set -e` writes its final marker from an EXIT trap, so a driver that dies early still leaves one.

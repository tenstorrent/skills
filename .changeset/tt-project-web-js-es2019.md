---
"tt-project": patch
---

The web app script no longer uses `??`, so older browsers and system node can parse it. A test checks this with `node --check` when node is installed.

---
"tt-project": patch
---

`tt-project`: `ttp stats --coord-session` tells apart why a coordinator session started anew. A transcript that is gone since a daemon restart is its own cause ("new: transcript gone after daemon restart"). Each session wake records a digest per part of its prompt prefix (the role template, the charter with its Restrictions, the standing settings that pick the role's rule blocks, and the rest: action schema and agent), and a new session for a changed prefix names the parts that changed; the stats count them per day. When sessions roll over is unchanged.

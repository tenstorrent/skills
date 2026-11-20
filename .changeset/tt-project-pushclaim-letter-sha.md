---
"tt-project": patch
---

`tt-project`: the push-claim audit reads a claimed short sha that has no digit (about 1 in 1,000 at 7 characters), which it used to drop, so a pushed task finished from its on_pass could be reported as a gap. A hex-looking word now counts only when it resolves to a commit it prefixes, never through a ref of that name.

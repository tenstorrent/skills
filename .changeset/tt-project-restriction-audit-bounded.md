---
"tt-project": patch
---

`tt-project`: the restriction audit that runs after a Restrictions item is loosened no longer stalls the daemon on a large tool tree or a long item. It reads each tool line once into a word index instead of running one search per word per line, so 50,000 lines against 300 words take well under a second instead of minutes. It also stops after 5 seconds: it keeps the tools it found so far and notes in the turn's results that the scan was cut short.

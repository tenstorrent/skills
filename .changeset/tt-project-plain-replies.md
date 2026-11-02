---
"tt-project": patch
---

`tt-project`: plain replies. The coordinator prompt asks for the answer in the first sentence, about 800 chars at most with details on request, and the harness described by what it does rather than its internal names. A model-free check (`plainreply.py`) flags a reply or notify with more than `coordinator.reply_max_terms` (1) unexplained internal terms (list in code, `coordinator.reply_terms` replaces it) or longer than `coordinator.reply_soft_chars` (800) without an answer first; flags show in the next Course check and nothing is blocked. The daily audit reports the median reply length and the flagged share.

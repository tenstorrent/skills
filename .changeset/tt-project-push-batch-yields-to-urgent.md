---
"tt-project": patch
---

`tt-project`: an urgent approval (priority 1, an `urgent:` line, or a review of such a task) no longer waits behind a push batch that is searching for the entry that broke its checks. Before each further check of the search (bisect probe, tip-alone check, re-run or rescue), the batch looks for such an approval for its target; if one waits, it runs no further check, pushes the entries that already passed, keeps any blame it already settled, and requeues the rest marked "yielded" (never blamed). The next batch takes the urgent approval alone, then the requeued entries go together again and their search finds the culprit. Routine approvals arriving mid-search change nothing. Only one batch writes the push branch at a time, as before.

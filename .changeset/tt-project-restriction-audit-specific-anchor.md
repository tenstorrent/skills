---
"tt-project": patch
---

`tt-project`: the restriction audit no longer queues an update task for a changed or retired Restrictions item from tool lines that share only generic words with it. Before, any two shared rule-word stems that were rare in the project's tool tree counted as a match, so a schedule description saying a result "remains valid" and that restoring "requires machine admission" was named as still enforcing a retired ban on one machine. Now a word-only match needs a specific anchor among its words: the machine, alias or resource the item names, or another distinctive term of it, such as its condition or clearance terms. Common verbs of state, need and validity and generic nouns (machine, device, result, ...) never count as one, however many match. Identifiers, quoted phrases and cites still match on their own.

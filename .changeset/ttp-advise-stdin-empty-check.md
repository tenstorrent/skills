---
"tt-project": patch
---

`ttp advise -` refuses an empty or whitespace-only question from stdin before it spends an advisor call, and refuses at once when stdin is a terminal instead of waiting on it.

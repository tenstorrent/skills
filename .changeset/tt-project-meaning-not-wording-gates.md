---
"tt-project": patch
---

Coordinator ask and memory gates judge meaning, not wording. An ask that a settled ask or a logged
order may already answer is sent with a flag (a note and the daily audit) instead of being refused.
An ask whose `classify` disagrees with its `blocking` is corrected to the class `blocking` implies,
with a note. The verbatim check on the user's words ignores case, spacing, quote style and
punctuation; a memory_add line that still fails is stored as data, relabelled so it never counts as
the user's words, and the rest of the entry applies (a rule entry with such a line is kept as a
non-standing fact). Restrictions changes, relayed rules and `neither` asks are still refused.

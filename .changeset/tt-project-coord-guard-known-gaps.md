---
"tt-project": patch
---

`tt-project`: the guide and the session coordinator's protected-file guard now list two more known gaps: a guard check that runs between a `ttp` write and its log entry puts that write back (saved and reported), and a `ttp` write landing between the guard's read and its put-back is overwritten unsaved; and a put-back restores the pre-call file, so it also undoes a logged `ttp` write made earlier in the same call (the combined content is saved). No change in behaviour.

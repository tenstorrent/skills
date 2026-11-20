---
"tt-project": patch
---

tt-project: the first worker start after the push branch's tip moves no longer reads each finished branch's landed state again: heads found off the push branch at the old tip stay off when none of the new commits shares their commits, files, author/date/subject or a replay trailer (three batched git reads for all heads).

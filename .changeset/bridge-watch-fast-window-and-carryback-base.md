---
"tt-project": patch
---

`ttp bridge watch` checks every few seconds for up to two minutes after a network change, ignoring the
backoff, until every box answers again, so the access bridge heals within about a minute of the
workstation being awake and online; the steady 30 s check and its 60 s backoff cap are unchanged after
that window.

A code task is no longer sent back for another task's rejected commits that were already on the base it
was told to build on (its `pr_branch:`, a stacked start or a fix chain's reviewed head): only commits it
brought in itself send it back. Its review still names the base's commits as inherited.

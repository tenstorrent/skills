---
"tt-project": patch
---

`tt-project`: hand-off follow-ups titled `upstream: ...` are filed, deduplicated by a fingerprint of
title and spec, in the user's inbox `~/.tt-project/upstream.jsonl`. A project with
`upstream.ingest: true` (off by default) reads this machine's inbox every minute and its remote
projects' inboxes over ssh at most hourly, with its own cursor, and turns new notes into
`upstream_note` events for its coordinator; `ttp status` and the web app show how many are not yet
read. Other coordinators pass upstream notes on to the user only while no project has read the inbox
in the last two days.

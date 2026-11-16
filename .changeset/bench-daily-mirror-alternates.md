---
"tt-project": patch
---

`tt-project`: `ttp bench daily` prepares each project's mirror without fetching every ref. The bench-owned mirror now reads the source repository's objects in place through `objects/info/alternates` and holds only a copy of its branch refs (a refresh rewrites those refs; nothing is cloned or fetched, and the source is never written to). A mirror made by the earlier full clone is converted in place. A project whose mirror cannot be prepared is still left out of that day's bench, but the invocation that started the run now prints one schedule observation for it (severity normal, key `bench-daily-skip-<project>`) once the pick stage is done, so the skip is no longer only a log line; the other projects run as before.

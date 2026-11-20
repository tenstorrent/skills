---
"tt-project": patch
---

`tt-project`: whole-file state writes now open their temporary file under a fresh random name with `O_CREAT|O_EXCL|O_NOFOLLOW`. A symlink or file planted at that name is never followed, written or removed; the write retries under a new name. Concurrent refused appends from several threads are now all kept for the daemon's `append_refused` events, and draining them never sees a torn list.

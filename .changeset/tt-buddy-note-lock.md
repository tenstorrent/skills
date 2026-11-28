---
"tt-buddy": patch
---

`note`: `write-entry.sh` takes its lock with a bash noclobber create instead of
`mkdir`. Some `mkdir` builds (Rust coreutils before 0.11) exit 0 when a racing
writer created the directory first, so two writers could commit in the notes
repo at once and fail on `index.lock`.

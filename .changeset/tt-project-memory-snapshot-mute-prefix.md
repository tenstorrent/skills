---
"tt-project": patch
---

The coordinator's memory is snapshotted so a memory change no longer breaks the cached system prompt on the next turn. Observation fingerprints keep digits inside identifiers, so different hosts no longer merge into one observation. A new coordinator action, `observation_mute`, silences a known recurring watcher observation. Memory `forget` and `supersedes` accept an exact name (live or archived) or a unique prefix, prefer a match ending on a word boundary, refuse anything ambiguous, and never retire the entry a `supersedes` just added.

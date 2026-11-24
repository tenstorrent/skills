---
"tt-project": patch
---

A landing recorded for a task that does not carry its change no longer counts as landed. The daemon checks each push-branch tip once: a done code task whose recorded landing holds neither its handed-off head nor most of its lines (a successor's rebase or batch left its commits out) is recorded as dropped with one coordinator event, and `landed:#<id>`, `ttp landed --task`, the ETA and tried-work ledgers stop counting it until it lands again or a supersession is recorded. A post-landing forward revert records its change as dropped too, and its fix task counts as re-landing it.

Re-landed changes a successor's rebase had left out: `ttp push --retire` runs with this host's own logged-in gh while no gh route is set; one helper records a deleted branch and the local retire path words its answer for this host; a hand-off whose why-this-size line sits mid-line is refused with the hint to put it on its own line; the detach-wrapper test waits for the wrapper to exit before checking its temp files.

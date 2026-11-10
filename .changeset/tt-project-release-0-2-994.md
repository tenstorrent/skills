---
"tt-project": patch
---

tt-project 0.2.994: Claude Haiku decides the daemon's cheap questions while Jev cannot. The wake gate, the wake tier, log and alert screening and babysitting used to go unanswered whenever Jev was off, had no key or its key was refused, so every one of them fell back to waking a full model run. They now go to Jev while it is on and accepts its key, else to Claude Haiku at low effort through the Claude CLI with no tools and no session, answering only yes/no or a score. An answer that cannot be read counts as a wake, never a skip. Each Haiku call is booked in the ledger (`haiku:<use>`) and in the Jev use log, so the existing review still switches off a use that does not pay for itself. A refused Jev key is retried after an hour, and a newly saved key is used at once; a project's own `jev.enabled` is never changed. `jev.haiku: false` turns Haiku off; effort and model picks stay Jev-only.

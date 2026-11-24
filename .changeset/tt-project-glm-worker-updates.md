---
"tt-project": patch
---

A chat-provider worker running under mini-swe-agent now gets coordinator updates mid-run, as a Claude worker does. That harness has no tool hook, so its guarded command layer hands over the unread part of `steer.md` after the next command's output, marks it read (`steer.offset`, `steer.reads`) and marks the run as hooked. The daemon's unread-update checks, the run view and urgent-message steering then read such a run's offset as they do a Claude run's, and `ttp note` and `ttp steer` leave the hand-over to the command layer. `restart:` lines already stopped these runs the same way; a test now covers it.

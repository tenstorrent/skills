---
"tt-project": patch
---

Urgent messages: a coordinator wake holding an urgent message now starts with an "Urgent: post first" section naming the exact `ttp urgent-reply <chat> "<text>"` to run before it reads anything; a turn that has it but posted nothing by `urgent_msg.nudge_s` (150 s) gets one update telling it to post what it knows now; and with still no reply by `urgent_msg.hold_s` (270 s) the sender's chat gets a holding note (not a finding) in place of the late notice at 5 minutes. Turns that post early see none of it.

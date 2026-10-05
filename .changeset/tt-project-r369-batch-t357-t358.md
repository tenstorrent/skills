---
"tt-project": patch
---

`tt-project`:

- batch routine coordinator wakes while no slot would idle (coordinator.batch_s)
- hold routine wakes only while queued work fills every free slot; add ttp.replay

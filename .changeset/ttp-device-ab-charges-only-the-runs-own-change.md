---
"tt-project": patch
---

Device A/B stops the test arm only for an incident the run could have caused. A test-arm run is charged only when its own tool log holds a command that changed a host's state (a reset, power cycle, flash, reboot, service change or recorded machine change); a run that only read state, or has no readable log, is not. An incident inside another task's machine-change ledger entry on that machine (a planned reboot, deploy or power cycle) is not charged either. A re-armed A/B skips incidents from before the re-arm, while a run that started earlier is still charged for one after it. Each tick's recheck lifts a stop recorded under the old rule and says why.

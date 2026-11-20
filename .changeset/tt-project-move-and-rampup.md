---
"tt-project": patch
---

A planned project move (`ttp move --to/--from`) stops the old copy within minutes of the new one running, and the new copy starts workers only once the old one has stopped. After an outage ends (a reboot, a resumed pause, a provider back), new workers ramp up from a few and double each step, urgent work first, instead of all starting at once.

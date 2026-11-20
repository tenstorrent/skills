---
"tt-project": patch
---

`tt-project`: the persistent coordinator session (`coordinator.session.mode`) is now on by default, so every project with no value of its own, new or upgraded, runs its coordinator as one persistent session. A wake still falls back to the stateless turn when a session fails, its failure cap pauses it or the spend guard trips. The session code, the spend guard and the session tools read an unset mode the same way. Turn it off for one project with `ttp config <name> coordinator.session.mode off`; an explicit value is kept.

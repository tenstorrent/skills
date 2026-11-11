---
"tt-project": patch
---

The daemon no longer reads the charter's Policies wording to apply a granted delivery setting: the coordinator applies it with `config_set` in the turn carrying the user's word, and the daemon only raises one `grant_mismatch` event when a setting the user set was changed since. A setting hold now sends one low-severity proposal per setting state (the setting, the held tasks, the config change that releases them) while other ready work keeps dispatching.

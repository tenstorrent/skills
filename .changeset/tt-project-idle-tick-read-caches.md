---
"tt-project": patch
---

`tt-project`: an idle daemon reads less. `git remote` and `git remote get-url` answers are kept until a git config file changes (never for a config that includes other files). Ancestry checks on full commit ids come from one `git rev-list` per branch tip, grown from the previous tip when the branch moves forward, so several commits against one tip cost one git call and then none; a whole landed answer for two full ids is read once. machines.json, project.json and the account settings.json are parsed once per change instead of a dozen times per tick, and each caller still gets its own copy. Shallow repositories, grafts, replace refs and files changed in the last 2 seconds are always read fresh.

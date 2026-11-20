---
"tt-project": patch
---

`tt-project`: `ttp hours --check --present M` passes only if the user wrote in the project's chat within the last M minutes, and otherwise exits 1 with one line saying when they last wrote, in the home zone. The worker prompt and the setup-box sign-in guidance run a user step with a deadline under 10 minutes (a 5-minute MCP sign-in link) only when both the window check and `--present 10` pass; otherwise the task waits on that check and never sends the link on a timer.

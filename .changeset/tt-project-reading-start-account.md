---
"tt-project": patch
---

`tt-project`: a finished run's plan-window reading is filed under the account the run started on, since the CLI keeps the token it started with. A login switch during the run no longer puts the old account's window (and its red alert) on the new account. The reading goes to the new login only when the run shows it used that login: its stream initialized again, or the reading's reset time matches the new account's own last reading and not the start account's. A run with no start account recorded still uses the login at its end.

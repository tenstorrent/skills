---
"tt-project": patch
---

tt-project 0.2.997: a run that made no model call books $0. A CLI that was logged out or offline and retried until its timeout used to be booked the elapsed share of its budget (about $3.59 for each 41-minute timeout), which counted as actual spend and held the budget gate. A run with no tokens and no answer in its stream (no assistant message, item, completed turn or result) now books $0 and moves no cap. A one-time re-book takes back such estimates from the last 7 days with negative ledger rows. Three such failed runs in a row on one provider open its login breaker: nothing starts on it, the coordinator included, and one alert says it is logged out or cannot reach its API host and that logging in or restoring the network is the user's decision. It closes by itself once a model-free check passes: the CLI's login status and an HTTPS check of the API host. Tasks pinned to it fail over as they do for any logged-out provider.

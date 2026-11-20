---
"tt-project": patch
---

`tt-project`:

- blocker sweep skip: when every stuck item ends without a turn (a user pause, a time ahead, a fresh "not yet" from its own probe within its limits, an open ask, a free resource, a review within its expected time) and nothing changed since the last model sweep, the daemon skips the sweep turn model-free and writes a receipt naming each item and its end; the receipt resets the 6-h sweep clock, `ttp status` and the web feed show it as one low line, and the daily review counts model and skipped sweeps with the estimated saving. A new or changed item, a broken or stale probe, an overdue wait or deferral, a coordinator-set pause, a dead dependency, a late review or push, a pending signal or message, a re-check a turn asked for, or a model sweep 24 h old still runs the turn.

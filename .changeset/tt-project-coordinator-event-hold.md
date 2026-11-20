---
"tt-project": patch
---

`tt-project`: coordinator turns whose only triggers are routine events (task_done, followup_proposed, upstream_note, checks_slot_wait, task_waiting at info, low or normal severity) wait until `coordinator.event_hold_s` (default 300 s; 0: off) after the last turn ended, so events that arrive meanwhile share one turn. User messages, high or critical events, every other event kind, and a free worker slot with no ready task start a turn at once. `python3 -m ttp.replay --event-hold-s 300` replays the hold over past turns.

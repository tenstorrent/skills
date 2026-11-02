---
"tt-project": patch
---

`tt-project`: `python3 -m ttp.replay --event-hold-s 300 --quiet-hold` replays a candidate rule that holds event-only batches of plain hand-offs, checks_slot_wait and task_waiting through a free worker slot (never an urgent task's event), and lists the real turns it would delay with the tasks they queued, the replies they sent and the actions they decided, plus the largest delay it adds before a task start. The daemon's event hold is unchanged.

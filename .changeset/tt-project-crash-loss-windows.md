---
"tt-project": patch
---

`tt-project`: nothing is lost or done twice when the daemon, a run's supervisor or the machine dies
mid-step.

- A worker's hand-off (`result.json`) counts however its run ended: supervisor killed, reboot,
  timeout, stall, budget or error. Only a cancel overrides it, so finished work is not redone.
- When a run's supervisor dies, the daemon ends the agent it left behind (TERM, then KILL) instead
  of letting it run on with no wall clock, budget or cancel beside the task's retry. The supervisor
  records the agent's start time, so a process that reused the pid is never signalled.
- A run the daemon recorded but never launched spends no attempt.
- A cancel cut off before it reached the run is completed on the daemon's next tick.
- A coordinator turn replayed after a crash writes its memory, charter section and worker updates
  once. They carry the turn that wrote them, so the same text from a later turn is still written.
- Slack: a reply in the thread of any post from the last 7 days is read, even after newer messages
  (within a minute for posts older than the last message read). Each thread keeps its own read
  position, and history is read page by page, so a backlog longer than one page is read whole. A
  message and its read position are saved together, so a crash cannot store it twice. Outbound
  posts stay at-least-once: a crash right after a post can repeat it.
- A budget level that changed while the daemon was down is announced after the restart.
- An alert is marked sent only together with its message.
- A mid-run update is marked read only after the hook has handed it to the worker.

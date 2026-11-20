---
"tt-project": patch
---

Coordinator obligation actions that change nothing owed are notes or auto-fixes, not rejections
(#2116, part 2). An owner that cannot carry an order (a question, plan or review task) or that has
already closed is left off with a note, and the rest of the action applies. A due move on a closed
obligation, or on an open one with no promise, is noted. An order linked to a closed obligation is
recorded as a new order. A task spec that names an obligation the task does not own attaches it; a
`task_add` whose kind cannot carry the order it names is still added, without the order. A bare
clock time in `start_after` (`15:17`, `15:17 PDT`) means its next time in the home zone. The CLI
still refuses these, and the coordinator still refuses a missed due without a plan change, an
unknown milestone (the message now lists the milestones), Restrictions changes without a source,
relayed rules, `neither` asks and guard edits.

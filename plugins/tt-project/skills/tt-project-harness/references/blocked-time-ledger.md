# Blocked-time ledger

The ledger says which rule held how much of the project's work, so the top blockers get retired or rewritten the same day.
Each row is `{rule, task, kind, start, end, minutes, safety, source}` from four sources:

- task blocked, waiting and review intervals (the rule is the first rule id the reason cites, else its first six words with digits and ids stripped);
- guard refusals in each run's `refusals.jsonl` and `glm_refusals.jsonl` (the rule is the refusal's `kind` or `rule`; minutes run until the task next ran, when that run handed off blocked or waiting, else 0);
- the queue-wait episodes;
- `state/blocklog.jsonl`, which `ttp block-log` appends to.

`safety` names the gate holding the time: one of the user-gated classes, a held resource, a check, a review or push, a user message or a rule id. Anything else is `convenience`, and convenience time is what gets fixed.

`ttp block-log <rule> [--task N] [--seconds S] [--why TEXT]` appends a line for time lost to a rule outside a task's blocked state.
`ttp blocked [--days N] [--json]` prints the top 10 rules by minutes with their safety class and tasks, and the blocked share of ordered work against the 5% target. Ordered work is a task the user added or cited, one answering a chat, one an open order owns, or priority 1; the share counts each task's blocked, waiting and review time once.
Ordered work is a task with a user message cited, an owner of an open order, or priority 1; overlapping rows of one task count once.
The daily review's unblocking lines carry one `Blocked time:` line with the share and the top convenience rules.

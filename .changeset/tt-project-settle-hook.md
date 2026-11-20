---
"tt-project": patch
---

`tt-project`: new `coordinator.settle_cmd` (empty, off by default; chat-settable). Before waking the coordinator for a batch of routine events (`task_done`, `upstream_note`, `observation`), the daemon passes them as JSON on stdin to the project's own command (from the project root, about 20 s, no model), which prints the events to settle with a reason. The runtime never settles user messages, failures, review results, promises and obligations, Must-act-now items, outage or machine-condition events, Restriction mentions or high severity, whatever the hook says. A hook error, timeout, bad JSON or non-zero exit settles nothing and raises one low deduplicated observation. Settled events are logged with their reason, shown in the web feed and counted in the daily self-efficiency audit. Documented in the tt-project-harness skill (`references/settle-hook.md`).

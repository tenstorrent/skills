---
"tt-project": patch
---

tt-project: the AI-authorship tag defaults to `[Authored by AI]` (matched in any case, so `[authored by AI]` still
passes, and a project's own `delivery.ai_tag` is kept). The prompts, the relay skill and the guide say it also goes
on every draft or text written for the user to post anywhere. A direct post through the claude.ai Slack connector
needs none, since the connector labels it as written by Claude; the connector guard exempts only those sends, not
its drafts. The PR-finishing specs the daemon writes name the project's tag instead of a fixed string.

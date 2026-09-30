---
"tt-project": patch
---

`tt-project`: status, blockers and spend are easier to read in `ttp status`, the web app and chat.

- Plan-window budgets list every window with its projection and reset time, and say that the
  dollar caps do not apply to plan-billed spend. The web header shows spend against the limit
  that binds (the worst window's use, or dollars of the daily and weekly caps).
- The web app's "Waiting on you" column shows why each task is blocked, and each open question's
  number and age; `ttp status` shows the question number and age too.
- `ttp status` and the web app's "Working now" show each running worker's task title, run time
  and its latest `ttp note`.
- A blocked task that replies to a chat includes what it needs from the user.

---
"tt-project": patch
---

`tt-project`: chats, desktop notifications and Slack no longer deliver a high alert late once
its condition has cleared.

- A chat listener, the desktop notifier or Slack catching up after being down skips high alerts
  (logged out, provider limit, budget red, disk low, coordinator failing, runs not starting) that
  no longer apply. Alerts that still hold, and lower-severity follow-ups, are delivered as before.
- The desktop notifier no longer stalls behind a long run of broadcasts below its severity floor.

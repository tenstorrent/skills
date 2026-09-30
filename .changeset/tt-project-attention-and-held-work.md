---
"tt-project": patch
---

`tt-project`: the web app stops showing alerts that no longer apply, and says why ready work is not starting.

- A high alert (logged out, provider limit, budget red, disk low, coordinator failing, runs not
  starting) leaves the web app's attention list once its condition clears or a newer alert on the
  same condition replaces it. Alerts whose condition cannot be checked still age out after a day.
- While runs are working, ready tasks held back by a paused provider, a red budget, low disk, a
  paused project or a stopped daemon are listed with the reason, in the web app and as a `held:`
  line in `ttp status`, together with anything waiting on the user.

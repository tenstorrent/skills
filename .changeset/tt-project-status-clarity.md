---
"tt-project": patch
---

`tt-project`: status and the web app show what the project is doing, spending and waiting for.

- `ttp status` adds spend over 24 hours and 7 days with the top spender, each budget gate's numbers
  against its caps or plan window, coordinator health (last turn, failures in a row, retry time,
  next idle check), paused providers with how to fix them, waiting tasks with their next try, and
  a one-line reason when nothing is running.
- `ttp status --json` and the web API carry the same fields under `health`.
- The web app header shows spend and how many things need the user; the overview shows why the
  project is idle and a coordinator health card. The "cannot reach the daemon" banner says how old
  the data on the page is.

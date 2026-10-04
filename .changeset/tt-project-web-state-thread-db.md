---
"tt-project": patch
---

The web app's page data no longer crashes once a project has had a coordinator turn. The next-wake check read the database through the daemon's main-thread connection from a web request thread, which SQLite refuses, so `/api/state` dropped every request and the page loaded empty. `idle_wake` and `wake_fingerprint` now take the caller's connection, and the web app passes its own.

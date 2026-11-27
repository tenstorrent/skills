---
"tt-project": patch
---

`ttp new` refuses a project folder inside a run's tmp folder before it writes anything (it used to create the folder and then fail to register it), and a name lookup that finds a remote chat-log hit inside a run's tmp folder skips it and tries the next hit.

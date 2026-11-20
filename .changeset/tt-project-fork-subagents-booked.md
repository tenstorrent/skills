---
"tt-project": patch
---

A Claude run that forks the coordinator's session books its own tokens at list price, because its reported total includes the parent session's cost. Until now that left out the run's subagent calls (Agent or Task tool): the result's usage counts the main thread only. Those calls now book with the run, each priced at its own model. They are read from the stream and from the session's subagent transcripts, which also hold the messages the stream leaves out. The run's note records their share as `subagent_usd`.

---
"tt-project": patch
---

`tt-project`: GLM runs tasks that stayed on Opus only because their result reaches a person, and Opus writes every word a person reads. A `conclusion:` line alone no longer keeps a run on Opus. A chat-answering task (`reply_chat`) runs on GLM, and for it the daemon never posts GLM text: a small Opus write-up step reads the hand-off as data and writes the message (claimcheck marks unproven claims; `message_by` stamp), and a post guard refuses unstamped chat-provider text on every path. A failed or held write-up posts nothing and sends the task to the coordinator. `route.glm_work.person_writeup` (default on, only where the GLM route is on) turns it off. An instruction to post stays a draft for the coordinator. The GLM share report splits `person` into `person:reply_chat`, `person:instruction` and `person:conclusion`.

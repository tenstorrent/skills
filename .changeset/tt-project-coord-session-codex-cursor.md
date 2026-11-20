---
"tt-project": patch
---

The always-on coordinator session now runs on Codex and Cursor as well as Claude Code. Codex resumes
its own saved session (size and compaction read from its rollout) and gets the coordinator's tools
through its own hooks; without ttp's hook it gets no shell or web. Cursor resumes its chat when the
chat store is still there and starts fresh when it is gone. The spend guard, session status line and
wake budget follow each provider's own session support. The chat provider keeps stateless turns for
now, with the reason named in each wake. A coordinator turn on an agent without ttp's tool hooks gets
any charter, memory or project config change put back after the wake, also after a cut-off reap.

---
"tt-project": patch
---

`tt-project`:

- WIP bench: server-wait accounting, serverhealth, codex proxy (t8715)
- WIP bench: codex harness arm + planner arm wiring, chat probe and server rerun (t8715)
- bench: server-rerun of old daily stops, per-task opts on reruns, planner arm tests (t8715)
- tests: smoke entry for the server-wait cases (t8715)
- bench: serverhealth history and codex proxy files written durably (t8715)
- bridge: claude bridge streams chat-completion chunks and reads the XML parameter form of tool calls (t8715)
- bridge: one tool call per reply in the claude bridge prompt, both call forms parsed (t8715)
- bridge: the chat relay forwards only data lines of a stream, so named events and comments from an upstream no longer break an OpenAI client (t8715)

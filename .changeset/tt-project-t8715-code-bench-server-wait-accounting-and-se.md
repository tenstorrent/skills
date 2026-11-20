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
- bridge: the claude bridge mends a model's slips in the tool-call form (doubled opener, tool_call_args tag, stray closers, open last block, raw newlines in JSON) (t8715)
- bench: the cheat audit reads only a mini-swe-agent cell's own tool calls; a flagged cell is never landed and is listed apart; v1 label on the daily table
- bench daily: a flagged cell is not counted landed by the second grader either; test (t8715)

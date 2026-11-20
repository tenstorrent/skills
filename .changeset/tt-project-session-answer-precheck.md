---
"tt-project": patch
---

`tt-project`: a session coordinator's answer is checked before its turn ends. The StructuredOutput hook runs the checks the daemon would apply afterwards that need no side effect (an unknown task id, a dependency that failed or does not exist, the daily task caps, fields a running or finished task keeps, missing title or spec, an unknown kind) and sends a refused action back as the tool result, so the coordinator fixes or drops it in the same turn instead of at the next wake. Valid answers are unchanged; an action resent unchanged goes through and is refused as before; at most 2 answers a wake are sent back, and any error lets the answer through.

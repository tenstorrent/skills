---
"tt-project": patch
---

`tt-project`:

- the Claude CLI bridge for a mini-swe-agent bench arm also reads the model's own XML call form (`<invoke name>` with `<parameter name>` children) when the reply has no `<tool_call>` block, so a Claude cell no longer fails with a format error on a valid call; tests

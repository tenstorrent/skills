---
"tt-project": patch
---

`tt-project`: console model calls carry no output-token cap and are bounded by time. The wake pre-screen (babysit) and the model bench send no `max_tokens`, and the chat bridge drops the client's own, sending only a cap the user set (`providers.chat` `max_tokens`, or a tier's). A babysit answer the server still cuts off (empty, finish_reason `length`) asks the next listed model and the ranked stand-in before it falls back; if every one was cut off, the fallback line says so and no cool-down applies.

---
"tt-project": patch
---

`tt-project`:

- bench arms take a harness type (claude, opencode, mini-swe-agent) on the chat provider's OpenAI-compatible relay
- route.device_ab, an off-by-default Sonnet/Opus A/B on device tasks with an incident stop, masked reviews and ttp stats --device-ab
- exempt a bench harness install's npm/pip log from the durable-write check
- a device A/B test in the smoke list
- a bench harness arm's uncached input tokens count once (the bridge already leaves out the cached share)

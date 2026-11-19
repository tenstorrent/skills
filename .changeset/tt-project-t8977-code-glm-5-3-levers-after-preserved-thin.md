---
"tt-project": patch
---

`tt-project`:

- chat: host-wide in-flight cap (chat.max_inflight), refusal backoff, bad-output counts and ttp bench toolprobe
- inflight: low-priority chat calls (TTP_CHAT_PRIORITY=low: ttp bench, model-bench, the tool probe) wait while a normal call waits
- glmshare: GLM bad output by day (tool input errors, unparsed args, reasoning-only, junk; rate per answer, runs hit) in ttp stats --glm-share and the daily review
- chatbridge: vendor defaults behind providers.chat.vendor_defaults (off by default): GLM sampling, effort map, 1M context window, API_TIMEOUT_MS and CLAUDE_CODE_ATTRIBUTION_HEADER=0, each a lever; TTP_CHAT_VENDOR_DEFAULTS per run

---
"tt-project": patch
---

`tt-project`:

- chat: host-wide in-flight cap (chat.max_inflight), refusal backoff, bad-output counts and ttp bench toolprobe
- inflight: low-priority chat calls (TTP_CHAT_PRIORITY=low: ttp bench, model-bench, the tool probe) wait while a normal call waits

---
"tt-project": patch
---

An urgent message jumps the queue. It skips the coordinator's debounce and relay window, is read first in the digest and tagged urgent, and reaches a coordinator turn already running as a steer update that the hook hands over after its next tool call. A turn that cannot read updates, or has not read it within `urgent_msg.read_s`, is cut with a resumable stop (its messages and events stay queued) and the next turn resumes with the urgent message first. The same on every provider; new settings `urgent_msg.cut_after_s` and `urgent_msg.read_s`.

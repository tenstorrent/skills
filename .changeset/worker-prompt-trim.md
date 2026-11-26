---
"tt-project": patch
---

`tt-project`: the worker and code-task prompts drop about 300 characters of repeated wording (no rule removed, caps unchanged), and every prompt size cap test now runs in the pre-landing smoke suite, so a prompt edit that overshoots a cap fails before it lands.

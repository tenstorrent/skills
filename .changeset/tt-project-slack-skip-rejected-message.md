---
"tt-project": patch
---

`tt-project`: one message Slack refuses no longer stops all later Slack delivery.

- A message Slack rejects for its own content (for example empty or too long) is skipped after
  three tries, and the skip is logged. Later messages are then delivered in order.
- Network errors, HTTP errors, rate limits and auth errors never skip a message; delivery resumes
  from the same message once Slack is reachable again.

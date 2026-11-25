---
"tt-project": patch
---

`tt-project`: a GLM-routed code run whose route fell back to the core provider (a limit, quota or hold on the chat endpoint, or no endpoint set) now starts on the core provider's own harness instead of mini-swe-agent. Before, the run kept mini-swe-agent with no endpoint, failed at start with missing credentials, spent an attempt and was retried a tier up. A mini-swe-agent run now always has its bridge, so a start failure there ends as the free route fallback.

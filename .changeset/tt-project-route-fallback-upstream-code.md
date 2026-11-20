---
"tt-project": patch
---

`tt-project`: chat routing: a routed run's bridge stats and route entry keep the failed answer's upstream HTTP status and error code (for example 404 model_not_found, told apart from a 5xx), kept only as a plain code word, never the message, a key, a URL or a host; a routed run that fell back shows in the coordinator feed as one route-fallback line with its reason, not as a failed run with the endpoint's error text.

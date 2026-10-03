---
"tt-project": patch
---

`tt-project`: A logged-out probe that hangs or retries without spending no longer clears the logout alert
after 2 minutes. Only real evidence of a login ends it: a successful run, or a running probe that spends or
streams tokens (the live meter now records a running run's tokens as well as its cost). Before, a stuck
provider CLI released the whole queue and every run failed once on the logout.

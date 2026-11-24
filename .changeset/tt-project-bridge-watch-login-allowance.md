---
"tt-project": patch
---

The bridge watch no longer restarts a bridge session that is still logging in. When none of an agent session's forwarded ports has a listener yet and its ssh login (a relay reconnect included) started less than 30 s ago, the pass logs it as still logging in and leaves it to the next pass. A session whose login age cannot be read, or which has no ssh yet because it is in its random start wait, is restarted as before, and a session that survived a sleep is old, so it is still restarted on the first pass after a wake.

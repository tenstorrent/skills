---
"tt-project": patch
---

`tt-project`: the critical-path ETA (`ttp eta`, the coordinator's Expected finish lines, status and promise estimates) starts a queued or waiting task held by its provider's spend gate no earlier than the gate's known reset (a cap's budget-day end, a plan window's reset, the runaway guard's hour, a provider limit's end); a code task in review waits for its held review the same way. With no known reset (a rolling 24 h or 7 d cap) it gives no clock time and says the task is held by the spend cap. A coordinator turn guard's red holds only routine work here, as at dispatch. The urgent spend-held digest now names a reached daily cap's reset time when the budget day fixes one.

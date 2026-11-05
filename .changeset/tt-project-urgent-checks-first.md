---
"tt-project": patch
---

Urgent landing checks go first: the host-wide checks queue and the test pool's host line serve urgent runs (priority 1 or less, an `urgent:` line, landing gates) of any project ahead of routine runs whatever their arrival, most urgent then oldest, and a freed pool slot is kept for an urgent parked run of another project. A queued run whose task moved its branch to a new head is dropped. Spill hosts get slots from their measured idle cores and memory instead of a fixed cap of 4. An urgent run waiting 10 minutes in a host-wide queue raises one `urgent_check_wait` event naming the queue and its holders.

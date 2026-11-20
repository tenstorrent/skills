---
"tt-project": patch
---

`tt-project`:

- checks never start on a host whose 1-minute load is near its cores (`checkspill.LOAD_NEAR`, 0.8 x), read live before each start; this machine's runs started there in the last 90 s count as load already, since the 1-minute load shows them late, so a burst of runs fills a host only up to that line and then waits. Before, each claim made room for the next (its slots counted this machine's own claims back in) and a host already at 40 of 64 cores took 6 of 16 runs; now it takes 2 and the rest go to idler hosts.
- the spill pick tries the emptiest host first (its load plus this machine's runs there, over its cores), so each host's share follows its free cores, with no fixed per-host cap.
- a spill host whose probe fails (unreachable, ssh timeout, a failed probe) backs off for 60 s, doubling to 15 min, shared by every project on the machine; it is tried last meanwhile and its share goes to the other hosts. A probe that reads again ends the back-off.
- checks pinned to one host (a check's `"host"`, `delivery.checks_host`) never start there "anyway" past `checks.push_load_wait_s`: they wait while its demand, with the runs just started there from this machine, is at or above 0.8 x its cores (or `checks.load_guard` if lower), and say so again every `checks.push_load_wait_s`. Starts are recorded under a lock, so runs waiting together see each other.

---
"tt-project": patch
---

`tt-project`:

- `ttp checks --reap` also reaps each check host's orphaned remote checkouts of this machine (`delivery.checks_spill_hosts`, the account's pool, `delivery.checks_host`) now, not only at the daemon's next pass (`orphanchecks.reap_report`), and prints one line per host: found, live, reaped, skipped (another machine's checkout or one with no owner file, never touched); a host that is paused or that ssh cannot reach is skipped and reported, and the reap still exits 0

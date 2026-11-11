---
"tt-project": patch
---

`tt-project`: every spill host gets a model-free health check. The daemon probes each host in `delivery.checks_spill_hosts` and the account pool over ssh, in a thread. The probe checks that python3 is there, that the pinned checks env is made or reused, and that `<checks.spill_wrap_cmd> python3 -c 'import pytest'` exits 0. No model runs, and no claude CLI is needed on the host.

The result is shared by every project on the machine, so a shared host is probed once every 10 minutes, not once per project. A wrapper refusal (exit 75 after writing its `TTP_WRAP_REFUSED` file) counts as busy, not as a failure.

A failing host gets the spill quarantine with reason "health". It raises one event and stays out of `checkspill.pick` until the check passes again, which lifts it. A health pass never lifts a strikes quarantine. When every probed host fails at once, it is a config fault: one high event, and no host is taken out.

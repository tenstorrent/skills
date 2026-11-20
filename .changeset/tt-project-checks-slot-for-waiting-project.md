---
"tt-project": patch
---

`tt-project`: a full `ttp checks` run no longer takes a host-wide checks slot while an equally or more urgent run of its own project already holds one and still waits for its test slots (under the project's test-worker budget or the load guard). Before, one project's runs could fill most of the host's checks slots only to queue in their own project's line, so other projects' checks waited for a slot. `ttp checks --slot-status` now shows such a holder as waiting on its project's test-worker budget instead of "no host queue".

---
"tt-project": patch
---

tt-project: checks never start on an overloaded check host. Push and `ttp checks` runs whose checks took `delivery.checks_host` as their default host move to a spill host with free cores once the load wait passes (a light check that cannot spill runs here); with none free they keep waiting. A remote check that fails with a timing-sensitive test's failure while its host is overloaded is an environment fault of the host, not the change's failure.

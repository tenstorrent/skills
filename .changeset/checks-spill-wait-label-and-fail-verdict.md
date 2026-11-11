---
"tt-project": patch
---

tt-project: on a workstation that runs heavy checks only on spill hosts, a run waiting for a busy spill host now says so (`ttp checks --result`: "waiting ... holding no checks slot here for a busy spill host (...)") instead of "queued for a host-wide checks slot", and the queue-wait report files it under busy spill hosts, never offering a raise of the host-wide checks cap; checks that failed alike on two spill hosts (not the host's own fault or its env) now fail at once as the change's verdict, instead of moving from host to host and waiting for the last one

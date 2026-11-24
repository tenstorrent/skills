---
"tt-project": patch
---

`tt-project`: a deploy whose files are at the release while a live realtime stream on its host holds the project's daemon restart stays pending instead of paging "Deploy failed". Its re-check moves on with the hold, up to `delivery.deploy_restart_held_s` (default 7200) after the project fell behind; past that it pages once as a pending restart, with the hold's reason. Once the hold ends the daemon gets `delivery.deploy_restart_wait_s` to restart, and a newer release keeps that deadline. Files not upgraded, a failed upgrade or a dead daemon still page at once.

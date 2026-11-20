---
"tt-project": patch
---

`tt-project`: the deploy rollback alert and a failed requested restart now carry the condition key `redeploy:<project>` and clear by themselves once a later deploy on that host is verified running (the `ttp deployed` facts, any version, with this project's daemon started after the alert). `ttp alerts <project> --ack <id>` now closes an alert with no condition key by the id status and the web app show, instead of refusing it; the status line shows that id as `alert <id>`.

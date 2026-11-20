---
"tt-project": patch
---

`tt-project`: the Course check adds a `report-only follow-up` line for a correction whose only follow-up is llm schedules (every enabled schedule citing the correction or its fix task is an llm one). Such a watcher only writes reports, so before, the class counted as handled and could come back unseen. The line names the missing model-free gate and stays open until one exists: an enabled command schedule citing it, a done `harness` or `code` task citing it that adds a detector with a test, or a later fix recorded with `ttp task superseded`. It follows the `coordinator.course_check.recurring` setting. The tt-project-harness skill documents this in `references/correction-class-fixes.md`.

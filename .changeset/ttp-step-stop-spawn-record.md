---
"tt-project": patch
---

`tt-project`: a tick step's time-budget stop no longer lands between starting a detached process and recording it. Starting a worker's runner holds the stop from the spawn until the run's pid and runner.pid are written, so the run is no longer marked never started and its files removed while the runner keeps going untracked; the push queue's batch start holds it until the batch's pid is on its marker and its go is given; a passed check's on_pass command holds it until the command is tracked, so the next tick does not start it twice. Every spawn through the patched Popen also defers a stop until the child exists; the deferred stop fires right after. Dispatch also catches the stop itself (no Exception, so its start-failure handling missed it): a task set running whose run was never recorded goes back to the queue instead of staying running with no run.

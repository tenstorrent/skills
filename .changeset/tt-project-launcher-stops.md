---
"tt-project": patch
---

`tt-project`: `ttp detach --stop <job folder>` can now stop a job that a project's own launcher laid out its own way, with no detached.json entry and no pid file. The project registers the launcher's stop in project.json as `"detach": {"launcher_stops": {"<glob of job folders>": "<stop command>"}}`. The command gets the job folder as its last argument and is never run by a shell. The stop is logged in the job-signal stream and as a `detached_job_stopped` event. The command then checks that the job's processes are gone and names any that are left; a failed command or leftover processes exit 1, never reported as success. Folders outside the project root, the project's own folders, folders inside another tt-project project, and jobs with another project's processes are refused without running anything. An unregistered layout is still refused, and the message now names the config key. The kill guard's advice names this route.

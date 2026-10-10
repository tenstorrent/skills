---
"tt-project": patch
---

Each project now has a home time zone (`home_timezone` in its project.json), the IANA zone of the user's workstation. `ttp new` records it (from a workstation creating a project on another machine, the workstation's zone, not the box's); `ttp connect` from a workstation and its spend push update it, with one feed line per change. `budget.timezone` defaults to it, so the budget day follows the user's zone unless set explicitly. Existing projects get the account `budget.timezone` or the machine's zone once at daemon start. A new helper, `ttp/timefmt.py`, shows times in the home zone with its abbreviation (for example `2026-10-09 23:32 PDT`); `ttp status` shows the current time in it on its first line.

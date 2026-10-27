# Backend contract

A mode file must provide these sections. Stage skills call operations by name and never
assume a backend.

| Section | Provides |
|---|---|
| Acquire | Get a device: nothing for `local`; submit or allocate for the others. Returns a handle recorded in `run.json`. |
| Run | Run one command to completion on the device host, with the run environment, and return its exit code and log path. |
| Run in background | Start a long command and return a handle to poll, plus how to read its log while it runs. |
| Health check | Confirm the expected devices are visible and idle. |
| Reset | Reset the devices held by this run. |
| Kill | Stop this run's processes on the device host, and nothing else. |
| Collect | Make logs, profiles, and outputs available in the run root. |
| Release | Give the device back at the end of a stage. |
| Failure handling | Backend-specific failures and what to do about each. |

Environment: every command gets the variables recorded in `run.json` (checkout path, Python
environment, `TT_METAL_HOME`, and any stage-specific variables). Mode files say how they pass
them through.

Adding a backend: add its mode file with these sections, add a row to the table in `SKILL.md`,
and add a selection rule. No stage skill changes.

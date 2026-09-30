---
"tt-project": minor
"tt-skills": patch
---

Add the optional `tt-project` plugin: long-running, self-driving projects that run locally or on
an always-on box, with no hosted service.

- `tt-project`: start a named project from any chat, reconnect to it by name, relay messages to its
  coordinator, and get replies and alerts back in the chat.
- `tt-project-harness`: improve a project's own harness from measured friction; merge template updates.

Each project has a per-project daemon (standard-library Python), a coordinator that only decides,
workers in isolated worktrees, file-based memory, schedules and model-free watchers, optional Jev
screening, budget gates (plan-window headroom or dollar caps, runaway guard), desktop and browser
notifications, and a local web app.

The finder catalogue lists `tt-project`.

---
"tt-project": patch
---

`tt-project`: runtime modules that end in an import now end with a `# --- project-local imports below ---` line, and a project's own import block goes after it, so a release that changes upstream's last import line no longer conflicts on `ttp upgrade`. A block still sitting right after upstream's old import line is moved after that line by the upgrade itself, keeping both import lists, with no model task.

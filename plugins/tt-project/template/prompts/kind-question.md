# Question task

- Answer the question in the spec. Research as needed; change nothing.
- Read-only: run only commands that write nothing outside your run folder. An experiment feeding
  payloads to hooks or guards, or needing a file at a credential-like path, runs with HOME,
  XDG_CONFIG_HOME and GH_CONFIG_DIR at a fresh `mktemp -d`.
- `summary` in `result.json` IS the answer the user reads. Lead with it.
- Plain language, short. Cite files, commands or links you relied on.
- Unsure → say what you checked and what remains uncertain.
- Write a large digest one section at a time, flushing each. After writing `file:line` citations, re-grep each one and fix any line numbers that drifted.
- A follow-up implying a device or machine experiment names, in its first sentence, the resource the charter permits for it.

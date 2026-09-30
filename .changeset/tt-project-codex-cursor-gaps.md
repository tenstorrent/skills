---
"tt-project": patch
---

Close Codex and Cursor gaps behind Claude support:

- Codex coordinator turns send a strict `--output-schema` (closed objects, optional fields
  nullable) and drop the nulls before applying actions; the schema file is reused, not leaked.
- Codex workers may write the project's state folder and the repository's git folder, so
  result.json, `ttp note`, `ttp lock` and commits from a worktree work inside the sandbox.
- Codex and Cursor price tokens with the run's model and project.json `pricing.<provider>`;
  Codex no longer counts reasoning tokens twice.
- Codex and Cursor runs cut off before reporting usage are booked at the elapsed share of their
  budget instead of $0.
- Logged-out alerts and the web app give each provider's own login command; a bare "401" in
  unrelated output no longer pauses a provider.

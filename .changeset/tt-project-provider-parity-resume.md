---
"tt-project": patch
---

`tt-project`: Codex runs keep their `thread_id` and a lost Codex run resumes with
`codex exec [options] resume <id> -` when the CLI lists `resume` and its rollout is still under the
run's or the daemon's `CODEX_HOME`. Cursor workers load `plugin_dirs` with `--plugin-dir` and get
`--resume` argv when `--help` lists them (Cursor runs are not resumed yet). A Claude lost run is also
found under the run's own `CLAUDE_CONFIG_DIR`. A resume that failed before reporting tokens is free.
New `docs/providers.md` compares Claude Code, Codex and Cursor with links to their docs.

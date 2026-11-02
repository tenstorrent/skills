---
"tt-project": patch
---

`tt-project`: a `plugin_dirs` set for a provider that cannot load it (Codex; Cursor without `--plugin-dir`) raises one low alert per provider and value, never repeated on the next launch, and `ttp doctor` names the provider and says its workers run without those plugins (for isolated Codex workers, also without the plugins installed in config.toml).

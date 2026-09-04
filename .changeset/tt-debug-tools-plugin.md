---
"tt-debug-tools": minor
---

New plugin: one skill per Tenstorrent debug tool, teaching an agent to drive the
tool and read its output. Ships `tt-triage` and `tt-noc-dump` of a planned
seventeen, vendored from tt-metal's tool documentation and sources.

Skill tests live in the plugin and run a real agent behind `pytest -m agent`,
excluded from the per-PR suite. Answers are constrained by a JSON schema whose
fields are enums, lists and identifiers, so a test asserts an exact value rather
than matching phrasing. `env` is an array of name/value pairs because OpenAI
structured outputs reject free-form maps — a map validates on Claude and returns
400 on Codex. Dispatch is read from the transcript's Skill tool call and the
agent runs in an empty directory, so it cannot answer from the skill files
without loading them.

Interpretation tests consume output captured from a real device under
`tests/fixtures/`. None is captured yet, so those tests skip rather than pass.

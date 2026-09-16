# Codex status

The runner runs against either host:

```bash
python plugins/tt-debug-tools/evals/run.py                  # claude, the default
python plugins/tt-debug-tools/evals/run.py --host codex
```

The Codex leg **runs**. It does not **evaluate**: the skill content does not
reach the answer, so failures cannot be attributed to the skill rather than to
the host not loading it. Treat "the harness evaluates on Claude and Codex" as
false.

## Symptom

Codex passes evals answerable from priors — `verdict: no` on a wrong premise,
`evidence_strength: weak` on a clean result, reading a pasted artifact — and
fails every test that turns on a fact only the skill carries. It invents
variable names freely:

```
TT_METAL_DEVICE_KERNEL_DEBUG   TT_METAL_ENABLE_LL_KERNEL_ASSERT
TT_METAL_DPRINT_CORES_RISCVS   TT_METAL_NOC_SANITIZE
```

None of those exist. Asked to quote a skill's Traps section verbatim, Codex made
**zero tool calls** and replied that it has no read access to the skill file
content. So this is not the sandbox blocking a path — the content is simply
never loaded.

Installation is sound end to end — `codex plugin list` shows the plugin, the
cache holds every skill and reference, and `codex exec --output-schema` returns
schema-shaped answers. That is what makes the finding sharp: the plugin is in
place, and skill content still does not reach the model.

## Harness implication

`harness.py` reads dispatch from a `Skill` `tool_use` block in the transcript.
Codex has no such call and no observable substitute — not even a file read to
detect. So `AgentResult.dispatch_observable` is `False` for Codex and
`assert_dispatched` becomes a no-op there. `assert_invoked_tool` and the
substring content checks still run.

Re-run the Codex leg after any skill edit, and remember the cache: a repo edit
does not reach Codex until the plugin is removed and re-added.

## Gotchas that still hold

**`source.path` must be relative.** An absolute path is accepted by
`marketplace add` and the plugin then never appears in `codex plugin list`. No
error.

**Codex caches the plugin; repo edits do not reach it.** After every edit:

```bash
codex plugin remove tt-debug-tools --marketplace tt-debug-tools-dev
codex plugin add tt-debug-tools@tt-debug-tools-dev
```

`--marketplace` is required on remove. `marketplace upgrade` does not help — it
is for Git sources.

**`codex exec` needs `--skip-git-repo-check`** outside a trusted directory, and
`</dev/null` or it blocks reading stdin.

**Pass the bare schema to `--output-schema`.** Not the
`{"type":"json_schema",...}` envelope.

**A model-metadata warning is normal** and not a failure:
`Model metadata for 'azure/gpt-5.3-codex' not found. Defaulting to fallback
metadata`.

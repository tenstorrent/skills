# Maintaining tt-review-skills

These rules apply only to the canonical review catalogue under `skills/` and its generated
`plugins/tt-review-skills/` package. They do not constrain other plugins in this repository.

## Vendoring and provenance

Two upstreams are private while this repository is intended for public distribution. When bringing
text across, remove internal-only pointers, machine-specific or personal content, and anything a
disclosure owner asks to remove. Preserve useful public architecture detail and verify technical
claims against the code; provenance is not proof of correctness.

Record every source in `metadata.upstream`. Use a 40-character lowercase commit SHA and include
`repo`, `ref`, and `path` (plus `branch` when needed). Use `[]` for original work. Regenerate
`SOURCES.md` with:

```bash
python3 skills/meta/tt-skills-upstream-audit/scripts/check_drift.py --sources
```

The drift audit proposes updates; it never applies vendored changes automatically.

## gh-aw self-containment

gh-aw copies one skill folder. A review skill must not require a sibling skill, an MCP server,
hooks, an interactive prompt, hardware, symlinks, non-stdlib Python, or tooling absent from the
runner. `gh` is allowed when the consuming workflow grants it.

References must stay inside their skill folder. Duplicate a shared reference when necessary and
register the pair in `DUPLICATED` in `tests/test_skill_frontmatter.py` so CI checks equality.
Cross-skill mentions are composition guidance, not imports. The `meta/` bucket is exempt from
review-runner dependency constraints because it contains repository maintenance tooling.

## Review-skill invariants

`tests/test_skill_frontmatter.py` enforces these review-specific rules:

- names equal their directories and are globally unique;
- `metadata.tier` is `model`, `op`, `kernel`, or `process`;
- entrypoints are at most 130 lines and references are under 4500 bytes;
- referenced files exist, workflow pins resolve, and duplicated references match;
- review skills emit findings but never post them;
- every vendored repository is credited in the review documentation.

Buckets are `common`, `models`, `ttnn`, `metal`, `llk`, `inference`, and `meta`. Add promoted skills
to the review reference in the root README, regenerate the packaged plugin, and run `pytest tests/`.

# Maintaining tt-review-skills

These rules apply only to the canonical review catalogue under `skills/` and its generated
`plugins/tt-review-skills/` package. They do not constrain other plugins in this repository.

## Vendoring and provenance

Some upstreams have restricted visibility, as does this repository. Before moving content to a
broader audience, verify the applicable license and disclosure authorization. When bringing text
across, remove internal-only pointers, machine-specific or personal content, and anything a
disclosure owner asks to remove. Preserve useful public architecture detail and verify technical
claims against the code; provenance is not proof of correctness.

Record every source in `metadata.upstream`. Use a 40-character lowercase commit SHA and include
`repo`, `ref`, `path`, and `license` (plus `branch` when needed). Record the license at that
revision; `NOASSERTION` flags an unresolved license, not permission to redistribute. Use `[]` for
original work. Emit the per-skill provenance table with:

```bash
python3 skills/meta/tt-skills-upstream-audit/scripts/check_drift.py --notice
```

This requires PyYAML and authenticated `gh` access to the upstreams. Replace only the per-skill
table in `NOTICE` with the output; preserve the license notices and other attribution sections.
The command writes to stdout, not to `NOTICE`. The drift audit never applies vendored changes.

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
- referenced files exist, workflow skill names exist locally, and duplicated references match;
- review skills emit findings but never post them;
- every vendored repository is credited in the review documentation.

The workflow check does not verify remote SHAs or imports. Replace the example workflow's
placeholders and supply its shared imports before compiling it with gh-aw.

Buckets are `common`, `models`, `ttnn`, `metal`, `llk`, `inference`, and `meta`. Add promoted skills
to both their bucket README and the root README reference, regenerate the packaged plugin, and
run `pytest tests/`. The `meta/` maintenance skill is available from the checkout, not the plugin.

# Layer & Content Placement

- Where a tt-buddy skill lives.
- What it contains.
- Where each rule's canonical location is.

## Layer Placement

- Every skill belongs to exactly one layer.
- Declare it via `metadata.layer` in YAML frontmatter.
- Skills are flat under `skills/<name>/`. Layers are metadata only.

| Layer | Frontmatter value | Decision rule |
|---|---|---|
| Workflow | `metadata: { layer: workflow }` | Runs until a goal is met? |
| Tool | `metadata: { layer: tool }` | Does one concrete pipeline-bound thing (build, run)? |
| Meta | `metadata: { layer: meta }` | Cross-cutting utility, or builds/introspects tt-buddy? |

- Tool vs Meta: is it **pipeline-bound** or **cross-cutting**?
- Pipeline-bound: `tt-buddy:run` for execution.
- Cross-cutting, writes notes: `tt-buddy:learn`, `tt-buddy:note`, `tt-buddy:buddy`.
- Cross-cutting goes in Meta.

---

## Content Placement

| Content type | Where it goes | Rule |
|---|---|---|
| Procedural instructions | Skill (SKILL.md / sub-files) | Steps, decision trees, patterns |
| Per-repo execution patterns | `<plugin-root>/recipes/<repo>/` | Build, test, env, server lifecycle. Plain markdown, ≤60 lines. |
| Volatile info (APIs, patterns) | Nowhere — use `tt-buddy:learn` | Agent reads fresh from source on demand |
| Work products (findings, logs) | `~/.tt-buddy/notes` (git-tracked timeline per topic) | Invoke `tt-buddy:note` to write entries |

- **Cardinal rule: never inline volatile content.**
- Point to source files.
- Describe patterns and intent, not API signatures.

Wrong:
```
noc_async_read(uint32_t src_noc_addr, uint32_t dst_local_l1_addr, uint32_t size)
```

Right:
```
For NOC read/write API: tt-metal/tt_metal/hw/inc/api/dataflow/dataflow_api.h
```

### Repo prefix on source-code/doc paths

- Paths into a TT repo MUST carry the repo name.
- E.g. `tt-metal/ttnn/cpp/...`, `tt-metal/tech_reports/...`.
- Paths inside tt-buddy use `<plugin-root>/...`.
- Recipes under `<plugin-root>/recipes/<repo>/` already carry repo scope.
- Paths inside a recipe don't repeat the prefix.

### Plugin self-sufficiency

A skill may reference only:

- (a) its own sub-files,
- (b) `<plugin-root>/recipes/`,
- (c) other skills, by invocation,
- (d) artifact paths the skill owns, e.g. its note topic.

- Anything else is fetched at runtime, not declared.
- A skill MUST work with only tt-buddy + the source repo.
- Notes from other skills are NOT a dependency surface.
- NEVER reference files outside the plugin directory.

### No source line numbers

- Name files and stable identifiers only.
- Identifiers: functions, registers, env vars, log literals, codes.
- NEVER pin line numbers. They rot every refactor.
- `tt-buddy:learn` resolves current locations on demand.

---

## Single Canonical Location

- Content Placement picks the *directory*.
- This rule picks the *file*.

**Rule:**

- Each rule, command, path, or env var lives in one file.
- Other mentions are one-line cross-references.
- A cross-reference says *where*, not *what*.

**Heuristic:** which file updates first if this becomes wrong? That file owns it.

| Content | Canonical file | If this becomes wrong... |
|---|---|---|
| MCP routing rule | `skills/run/SKILL.md` | run skill updates first |
| Recovery step order | `skills/run/recovery.md` | recovery sub-file updates first |
| Note atomic-write protocol | `skills/note/protocol.md` | note protocol updates first |
| `build_metal.sh` invocation | `<plugin-root>/recipes/tt-metal/build.md` | recipe updates first |

**Cross-reference format** (in non-canonical files):

Wrong — restates the content:
> Triage must run before kill, while the process is alive.

Right — points to the canonical file:
> See `recovery.md` § Order.

- Readers who need detail follow the pointer.
- The rule forces the question. It does not replace judgment.
- On drift: ask the question and move the content.

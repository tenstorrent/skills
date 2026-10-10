---
name: static-op-analysis
description: Build the kernel-op table for a tt-metal model test by reading code at a pinned commit, tracing every ttnn call to its device op and program factory for P100, P150 and Quasar profiles, with validator-checked counts and CSV output. Use for "op table", "which kernel ops does model X use", or tt-metal#58113 criterion 3.
---

# Static op analysis

Reads code only; never touches a device. Every count must survive
`<plugin-root>/scripts/validate_static.py`, and every dispatch claim carries evidence.

`<plugin-root>` is the `tt-model-op-analysis` plugin directory, two levels above this
SKILL.md; resolve every `<plugin-root>/...` path against it.

## Steps

1. **Setup.** Follow `<plugin-root>/references/run-setup.md`: model, test, pinned SHA, output
   directory, overwrite/new/merge.
2. **Targets.** Ask which profiles: any of `p100`, `p150`, `quasar`. Load
   `<plugin-root>/references/targets.md`, confirm the values at the pinned SHA, write
   `targets` and `profile_values` to `run.json`.
3. **Inventory.** Read the model file and the test once, by range. List every ttnn call on the
   forward path (call site, stage, how many times it runs per inference) and every predicate from
   targets.md the code reads. Note host-side work for `host_ops.csv`.
4. **Trace.** Load `<plugin-root>/references/dispatch-tracing.md`. Small model: trace yourself.
   Larger model: one subagent per block with the subagent contract in that file. Collect rows for
   `op_table.csv` and `call_trace.csv` per `<plugin-root>/references/schemas.md`.
5. **Quasar.** If `quasar` is a target, load `<plugin-root>/references/quasar-status.md` and fill
   the Quasar columns and `quasar_blockers.csv`.
6. **Re-check.** Re-verify yourself every `unverified` row and every `❌` claim. Change the row
   only on new evidence; leave `unverified` if it cannot be settled statically and say why.
7. **Gate.** Run `python3 <plugin-root>/scripts/validate_static.py <run dir>`. Fix and rerun until
   it prints `OK`. No output, summary or upload before that.
8. **Summary.** Write `summary.md`: model, test, SHA, profiles, launches per inference per profile
   (copy the validator-checked totals), count of each status per column, top Quasar blockers,
   open `unverified` items. For a merge, add the changes section from run-setup.md.
9. **Upload.** Ask whether to upload. If yes, follow `<plugin-root>/references/drive-upload.md`.

## Rules

- Read sources as `git show <sha>:<path>` with line ranges; never the working tree.
- Write CSVs with a script or a heredoc into the run directory; never retype a table to fix it.
- A status of `✅` needs evidence a reader can open. When unsure, `⚠️` with the reason.
- Do not edit the tt-metal checkout.
- Report what the validator checked and what stayed `unverified`; do not claim a device run.

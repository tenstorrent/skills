---
name: autodebug
description: Debug difficult tt-metal and TTNN code issues by inspecting source in a fresh Codex or Claude session without running the target code. Use automatically when an installed plugin is available and an isolated deep investigation would protect the calling agent's context, especially before autofix. Do not edit source in the child session.
---

# AutoDebug

The launch instructions below are for the calling agent. If your prompt identifies
you as the already-isolated AutoDebug investigator, perform that investigation in
this session and write `AUTODEBUG.md`; do not launch AutoDebug again.

Run the bundled launcher. It renders the current AutoDebug prompt and starts a new agent process so
the investigation cannot consume or distort the calling agent's context window.

## Launch

1. Resolve `scripts/autodebug.sh` relative to this `SKILL.md`, not relative to the target repository.
2. Invoke that script by absolute path while the working directory is the checkout to investigate.
3. Pass the concrete problem after `--`. Add one or more `--focus <path>` arguments when they help
   bound the investigation. The launcher selects the current host automatically when run from an
   installed Codex or Claude plugin. Use `--agent` before `--` only to override that choice.
4. Do not ask for separate confirmation merely to launch AutoDebug. Installing this optional plugin
   enables normal implicit skill selection; the child remains inspection-only.

Example:

```bash
"<this-skill-directory>/scripts/autodebug.sh" --focus ttnn/cpp -- "Program-cache test hangs after the second trace"
```

Wait for the launcher to finish and read `AUTODEBUG.md` before ending your response. If you start it
in the background, wait for that task to complete. Verify the report's important claims against the
checkout and distinguish supported findings from suggested follow-ups. If the user's request includes
implementation, continue with `$autofix`; otherwise report the diagnosis.

## Codex sandbox startup

The launcher runs a no-model sandbox preflight before starting Codex. A successful check keeps
`workspace-write`. A failed check stops before a model starts.

If preflight or the child fails, inspect its diagnostics in context. The launcher does not classify
error strings or automatically retry without a sandbox. Fix unrelated launch, configuration, or
application errors rather than assuming sandboxing caused them.

When the failure warrants skipping the additional Codex child sandbox and existing user authorization
covers that execution, the calling agent may explicitly retry with `AUTODEBUG_SKIP_CHILD_SANDBOX=1`.
Do not ask again when that authorization is already clear; if it is missing, obtain it before retrying.
Docker or Slurm membership alone does not establish authorization.

The explicit skip bypasses preflight and selects `danger-full-access` with no approval prompts. It
adds no child sandbox: any restrictions inherited from the parent process still apply, but a parent
sandbox is not guaranteed to exist. Fresh-process investigation isolation, inspection-only instructions,
and model configuration are preserved. The setting applies only to Codex; Claude's permission mode
is unchanged.

## Invariants

- Do not replace the launcher with an in-context investigation. Isolation is part of this skill's
  correctness contract.
- Do not edit source in the child session. AutoDebug produces evidence; AutoFix owns changes.
- Do not assume silicon is unavailable to the calling agent. The AutoDebug child is intentionally
  inspection-only because its prompt must be portable across environments.
- Do not rely on a separate checkout of the prompt repository at runtime. The installed plugin is
  self-contained.

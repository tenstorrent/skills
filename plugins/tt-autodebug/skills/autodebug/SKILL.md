---
name: autodebug
description: Debug difficult tt-metal and TTNN code issues by inspecting source in a fresh Codex or Claude session without running the target code. Use automatically when an installed plugin is available and an isolated deep investigation would protect the calling agent's context, especially before autofix. Do not edit source in the child session.
---

# AutoDebug

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

The launcher waits for the fresh session to finish. Read `AUTODEBUG.md`, verify its important claims
against the checkout, and distinguish supported findings from suggested follow-ups. If the user's
request includes implementation, continue with `$autofix`; otherwise report the diagnosis.

## Codex sandbox startup

The launcher runs a no-model sandbox preflight before starting Codex. A successful check keeps
`workspace-write`. A failed check stops before a model starts.

On a machine where the user/operator has authorized unsandboxed work, they can set
`AUTODEBUG_ALLOW_UNSANDBOXED=1` in the launch environment. After a recognized Linux sandbox startup
failure, the launcher then warns and uses `danger-full-access` with no approval prompts. This
removes OS sandbox protection, including protection for mounted/shared data. It does not remove
fresh-process isolation or the inspection-only instructions. Other errors and timeouts still stop.

Do not set this variable yourself to get past a failure without user/operator authorization.
Docker or Slurm membership alone is not authorization. Claude's permission mode is unchanged.

## Invariants

- Do not replace the launcher with an in-context investigation. Isolation is part of this skill's
  correctness contract.
- Do not edit source in the child session. AutoDebug produces evidence; AutoFix owns changes.
- Do not assume silicon is unavailable to the calling agent. The AutoDebug child is intentionally
  inspection-only because its prompt must be portable across environments.
- Do not rely on a separate checkout of the prompt repository at runtime. The installed plugin is
  self-contained.

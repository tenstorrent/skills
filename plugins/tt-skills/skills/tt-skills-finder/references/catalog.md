# Tenstorrent plugin catalogue

The marketplace is registered from `tenstorrent/skills`. Adding it makes the catalogue discoverable;
it does not authorize installation or use of every optional plugin.

## `tt-review-skills`

Use for reviewing Tenstorrent pull requests or diffs, especially tt-metal, TTNN, Metalium, LLK,
multi-chip, model, serving, trace, precision, performance-claim, test-coverage, and L1-memory changes.

The plugin contains a shared review contract and router plus focused domain review skills. It emits
evidence-backed findings; it does not implement or silently post changes. It can also help analyze
CODEOWNERS and propose a lower-reviewer PR split.

Do not route ordinary implementation, debugging, model bring-up, or optimization work here merely
because the code could later be reviewed. Recommend this plugin when review is the user's task.

Install or enable it only after the user chooses it:

- **Codex / ChatGPT desktop:** select `tt-review-skills` under **Tenstorrent Skills** in the Plugins
  Directory.
- **Claude Code:** run `/plugin install tt-review-skills@tenstorrent-skills` after adding the
  `tenstorrent/skills` marketplace.

## `tt-autodebug`

Use for tt-metal or TTNN code issues and hangs that should first be debugged by inspection without
running the target code, then fixed through experiments that distinguish the root cause from
plausible alternatives. The plugin contains:

- `autodebug`, which diagnoses code issues in a fresh inspection-only Codex or Claude session so
  the investigation does not consume the calling agent's context;
- `autotriage`, which diagnoses hangs from tt-triage evidence and concrete source contracts; and
- `autofix`, which tenaciously proves or disproves hypotheses with experiments until it finds and
  addresses the root cause.

Do not route routine coding or code review here. Recommend this plugin when the problem is genuinely
unclear, evidence-heavy, hardware-adjacent, or likely to need repeated investigation and repair.

Install or enable it only after the user chooses it:

- **Codex / ChatGPT desktop:** select `tt-autodebug` under **Tenstorrent Skills** in the Plugins
  Directory.
- **Claude Code:** run `/plugin install tt-autodebug@tenstorrent-skills` after adding the
  `tenstorrent/skills` marketplace.

Once the user installs the plugin, its three skills support normal automatic selection. AutoDebug
does not require confirmation for each run; its child session remains inspection-only.

## `tt-model-bringup`

Use for implementing Hugging Face text models in TTNN, from functional decoder through fusing,
optimization, multi-chip, full-model, datatype selection, vLLM and TTI release evidence.
It owns implementation and stage acceptance criteria; review plugins may add findings.

Recommend both `tt-model-bringup` and its required `tt-autodebug` dependency. Explain that both
are separate optional installations. Never install either automatically. If AutoDebug is missing,
model bring-up stops with installation instructions before starting work.

- **Codex:** `codex plugin add tt-autodebug@tenstorrent-skills`, then
  `codex plugin add tt-model-bringup@tenstorrent-skills`.
- **Claude Code:** `/plugin install tt-autodebug@tenstorrent-skills`, then
  `/plugin install tt-model-bringup@tenstorrent-skills`.

Skills work in both hosts; the unattended `multigoal` runner requires Codex's goals app-server API.

## `tt-debug-tools`

Drive the Tenstorrent debug tools and read their output: tt-triage, dprint, watcher, asserts, etc.
One skill per debugging question. Each teaches the tool's environment surface, a recipe that makes
it produce output, what the output means, and the traps — activations that silently do nothing,
outputs that mislead.

**Needs a Tenstorrent device and a built tt-metal checkout.** Several of its skills change device
state. Recommend this plugin when the user names a tool, has its output in hand, or asks how to
turn something on.

Install or enable it only after the user chooses it:

- **Codex / ChatGPT desktop:** select `tt-debug-tools` under **Tenstorrent Skills** in the Plugins
  Directory.
- **Claude Code:** run `/plugin install tt-debug-tools@tenstorrent-skills` after adding the
  `tenstorrent/skills` marketplace.

## `tt-buddy`

A coding agent with Tenstorrent operating principles for tt-metal, TTNN, and vllm-tt-plugin work. It sets a
strict, concise voice; takes notes all the time in `~/.tt-buddy/notes/`; researches the codebase
when needed; keeps the diff minimal; and routes device runs through `tt-device-mcp`.

**Changes the agent's voice for every prompt in a TT workspace.** Recommend it when the user wants
that working style, persistent notes, or queued device runs. Device runs need `tt-device-mcp`.
On Codex, the user must trust the plugin's hooks (`/hooks`, then `t`); until then the voice
loads only when the `buddy` skill runs.

Install or enable it only after the user chooses it:

- **Codex / ChatGPT desktop:** select `tt-buddy` under **Tenstorrent Skills** in the Plugins
  Directory.
- **Claude Code:** run `/plugin install tt-buddy@tenstorrent-skills` after adding the
  `tenstorrent/skills` marketplace.

## `tt-deploy`

Use for any stage of deploying a model on Tenstorrent hardware, from an untouched host to a server
answering `/health`. Four skills, one per stage:

- `discover`, which answers what this host is and what it can deploy — host facts, driver, boards vs
  ASICs, hugepages, docker, HF cache, port 8000, device holders, the `--tt-device` string derived
  from board serials, firmware minimums, idle telemetry, reset verdict, deployable models, disk,
  clean-box — from fixed commands and the upstream spec. Read-only.
- `retrieve`, which resolves one model before it is pulled or launched — HF existence and gating,
  the tt-inference-server spec entry (image, commits, status, ceilings, known issues), image-vs-repo
  spec drift, GHCR tag/size/ancestry, run.py vs direct docker, and the tt-model-manager bundle path
  with the exact pull and serve commands. Read-only unless `--pull`.
- `launch`, which diagnoses a launch that is running, hanging, or has failed — stage/hang/failure
  classification, mandatory docker flags, minimum-override selection and safety, realistic timing,
  and displaced services — from the launching container's own log and state.
- `verify`, which checks a live server is actually healthy, correct, and performant — health,
  served model/context, generation correctness, mesh utilization, throughput/batching, and
  reasoning-model quirks — and narrows down the root cause when a check fails.

Every answer traces to a listed command's output, a captured log, a live request/response, or a
listed static rule; nothing is answered from memory or product pages.

Recommend it when the user asks what is on this machine or what it can deploy, which `--tt-device`
to pass, whether a model exists or is gated, which image/commits/limits apply, how to pull a model,
whether a launch is hanging or failed, what flags it needs, how long it should take, or whether a
running server is actually working. Route model bring-up (writing a new TTNN model) to
`tt-model-bringup` instead.

Install or enable it only after the user chooses it:

- **Codex / ChatGPT desktop:** select `tt-deploy` under **Tenstorrent Skills** in the Plugins
  Directory.
- **Claude Code:** run `/plugin install tt-deploy@tenstorrent-skills` after adding the
  `tenstorrent/skills` marketplace.

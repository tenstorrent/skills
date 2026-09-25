# Getting started with Agentic Research skills

We publish our skills at [tenstorrent/skills](https://github.com/tenstorrent/skills). Claude Code and Codex understand how to install plugins from this repo automatically, just give them the link and ask them to (more details below). A plugin is just a term for a collection of related skills that can be enabled/disabled together.

| Plugin | Details |
| --- | --- |
| `tt-autodebug` | AutoDebug, AutoTriage and AutoFix skills - find 80% of tt-metal bugs by inspection, fix bugs with a principled process of experimentation |
| `tt-model-bringup` | Fully-automated HF model to TTNN bringup including vLLM integration and benchmarking. Includes skills you can also use interactively for optimization, multichip parallelization, tracing, vLLM integration, and TTI release handoff. Requires a separately installed and enabled `tt-autodebug`. |

For broader direction and priorities, see the [AR Roadmap](https://docs.google.com/presentation/d/1TODRqfYb3Muw_pmlpZXw_emKx6xjY1i17t8aHPLuRfk/edit).

## Set up

Just tell your agent to:

```text
Install tenstorrent/skills
```

and it should be able to figure it out. It may ask you to restart the session after install.

*This doesn't replace or interfere with any of your skills by default* - instead it adds a single "plugin finder" skill that asks your permission before adding anything. You are always in control of which plugin you enable/disable at any one time.

### Alternative manual install:

Codex, in a terminal:

```bash
codex plugin marketplace add git@github.com:tenstorrent/skills.git
codex plugin add tt-autodebug@tenstorrent-skills
# Add this for model bring-up:
codex plugin add tt-model-bringup@tenstorrent-skills
```

Claude Code, inside the session:

```bash
/plugin marketplace add git@github.com:tenstorrent/skills.git
/plugin install tt-autodebug@tenstorrent-skills
# Add this for model bring-up:
/plugin install tt-model-bringup@tenstorrent-skills
```

## AutoDebug

AutoDebug launches a fresh investigation that reads the affected checkout and writes a beautiful **`AUTODEBUG.md`** report with root-cause hypotheses and evidence. It *only reads the code* so you can run it safely anywhere including on your laptop. **It solves 80% of tt-metal issues** in our nightly backtests. [Example report on a real ticket](https://github.com/tenstorrent/tt-metal/issues/44366#issuecomment-4520032209).

Just tell your agent to use it:

```text
Use AutoDebug: <symptoms>
```

Allow roughly half an hour.

**Ghetto-hosted option:** comment `@yieldthought autodebug` on a tt-metal GitHub ticket to request a hosted investigation. YMMV I let agents monitor and keep this up but if I run out of quota so do you ^^.

### Automatically fix a bug

If you are on the machine with the problem you can use AutoFix to loop through all the proposed hypotheses testing each of them on device until a fix is found:

```text
Use AutoFix: <reproducer and symptoms>
```

Just fire and forget on a tricky problem overnight. The model bringup skills (see below) use this and I haven't seen it fail to fix a problem yet, but I'd still consider it a bit experimental compared to AutoDebug which Just Works(tm).

## Model bring-up

Fire-and-forget model bringup. Set it running and come back 1-5 days later to a pretty well-optimized TTNN model served via vLLM. *This burns tokens*. I recommend the OpenAI Pro subscription for open-source model work, you can run a couple of these per week for $200/mo. A single bringup with API costs seems to clock in at a few hundred dollars. As of 09-2026 frontier models like Astra + Fable still do this best. Opus 5 and Sol 5.6 xhigh are also capable of model bringup. I do not recommend weaker models.

You can run this in two ways:

1. Directly on the machine - tell your agent to `Use tt-model-bringup on <HF model ID>`. Add any extra context, e.g. if it's a QB2 and you want it to use all the chips or only one of them or whatever it's fine to say so. By default it should do TP on all the available chips but asking for what you want doesn't hurt.
2. With a watchdog agent - this is how we run *all* of our model bringup experiments. Our laptop agents have skills to reserve and manage remote hardware, so we tell one agent to reserve a machine, check out and build the right tt-metal branch then run model bringup and check it every hour. The bundled skills tell watchdog agents what the requirements are for this but do not cover machine reservations.

Approach (1) should be fine, but in practice unexpected things can and do happen over a multi-day period. For example, with (2) our managing agents will refresh the reservation as necessary, recover from problems like running out of disk quota or even migrate the bringup to another machine if a device gets so wedged it can't even reset cleanly.

For simplicity I recommend starting with approach (1) for your first model bringup, but be aware that approach (2) exists and is how we usually run these things for larger numbers of models.

For Codex multigoal runs, agents automatically install and use the telemetry plugin from
`tenstorrent/ar-dashboard` if the execution host already has access. This provides a local
progress HTML report and dashboard delivery. Without access, bringup continues quietly;
no login or extra setup is needed. You can explicitly opt out of telemetry. When resuming,
reuse the recorded launcher and telemetry environment; the runner does not automatically
restore the optional integration. See the
[telemetry setup procedure](../../plugins/tt-model-bringup/skills/model-bringup/references/telemetry-setup.md).

### How model bringup works

It takes the current tt-metal checkout and uses that version to bring up the model in separate stages:

| Stages | Main result |
| :-: | --- |
| 1–3 | Functional, fused, then optimized decoder; HF comparison and warmed performance evidence |
| 4–5 | Multi-chip decoder and multi-chip optimization |
| 6–8 | Full model, optimized generation, and a datatype/fidelity sweep |
| 9–11 | vLLM integration, optimized serving, and accuracy/performance benchmarks |

Code and tests are created under `models/autoports/<model>/` plus a lot of profiling dumps and other information. Tell a separate agent to monitor this and create a HTML dashboard to show you the progress!

It will install and patch vLLM and so on as necessary. Give it appropriately wide-ranging permissions.


## Prevent your agent using these skills on its own

Once you've installed a skill such as AutoDebug, your agent may decide to use it during your own daily work. I think this is great! However, if you prefer direct control over these things, paste the following into your chat session:

```text
Make AutoDebug, AutoTriage, and AutoFix explicit-only for me. Set each resolved
skill's Codex allow_implicit_invocation policy to false, or Claude
disable-model-invocation frontmatter to true. Preserve explicit invocation,
verify the active installation, and explain how to retain this after updates.
```

In Codex this is `policy.allow_implicit_invocation: false` in each skill's `agents/openai.yaml`; in Claude it is `disable-model-invocation: true` in each `SKILL.md` frontmatter. Cached edits may be replaced by plugin updates; recheck after updating. See [Codex skill controls](https://learn.chatgpt.com/docs/build-skills) and [Claude skill controls](https://code.claude.com/docs/en/skills).

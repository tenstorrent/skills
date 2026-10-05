---
"tt-serve-model": minor
"tt-skills": minor
---

Add the optional `tt-serve-model` plugin with its `discover`, `retrieve`, `launch` and
`verify` skills, covering all four stages of the model-deployment journey.

`launch` diagnoses a model launch that is running, hanging, or has failed —
stage/hang/failure classification, mandatory docker flags, minimum-override
selection and safety, override persistence, realistic timing, and displaced
services — from a single captured `docker inspect`/`docker logs` snapshot of
the launching container.

`verify` checks a live server against a fixed table (health, served
model/context, generation correctness, mesh utilization, throughput/batching,
reasoning-model quirks) and narrows down the root cause when a check fails.

Both skills, and the new shared `knowledge/recipes/tt-inference-server/container.md`
reference, were verified live on a real QuietBox 2 (p300x2): a first attempt
(Qwen3-32B, image 0.10.x) hit a real, reproducible tt-inference-server
entrypoint bug before reaching `/health`, and a later attempt
(Qwen3.6-27B, image 0.20.0) reached `/health` 200 and exercised `verify`
end to end. Real findings folded in: measured per-phase launch timing across
a cold and a warm run (the published "8-22 min cold / 6-8 min warm" estimate
does not hold), a silent device-misdetection fallback that degrades prefill
chunk size, a `.Mounts`-vs-`.HostConfig.Binds` false negative in the
mandatory-flag check, a SIGBUS crash after a long idle period, and a
reasoning-model case where `reasoning_content` stays null even though the
parser is wired.

`discover` answers the Discovery-stage host questions (D1–D16) from fixed
commands: host facts, driver, boards vs ASICs, hugepages, docker, HF cache,
port 8000, device holders, the `--tt-device` string derived from board serials,
firmware minimums, idle telemetry, a reset verdict, deployable models, gating,
disk budget, and clean-box. Read-only; the reset itself is never run here.

`retrieve` answers the Model Retrieval-stage questions (R1–R16, B1–B20) for one
model: HF existence, gating and config; the tt-inference-server spec entry
(image, commits, status, ceilings, known_issues); image-vs-repo spec drift;
GHCR tag, size and ancestry; run.py vs direct docker; and the tt-model-manager
bundle path (search, catalog, manifest, weights pointer, install state, exact
serve command). Read-only unless `--pull`.

The shared `knowledge/` tree grows `hf-hub.md`, `hardware/boards.md` and
`recipes/tt-inference-server/model-spec.md`, which the four skills cite.

The finder catalogue gains `tt-serve-model` alongside the other optional plugins.

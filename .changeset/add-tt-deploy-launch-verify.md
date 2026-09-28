---
"tt-deploy": minor
"tt-skills": minor
---

Add the optional `tt-deploy` plugin with its `launch` and `verify` skills.

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

Discovery (what the host is) and model retrieval (what the model/image
should be) are a separate, already-drafted pair of skills for the same
deploy-model journey and land in a fast-follow PR, so `tt-deploy` grows to
all four lifecycle stages without blocking this one on that work.

The finder catalogue gains `tt-deploy` alongside the other optional plugins.

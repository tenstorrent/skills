---
"tt-project": patch
---

`tt-project`: an effort A/B for standard-tier code tasks (`effort_ab.enabled`, off by default, chat-settable): each code task queued at standard with no effort pin goes in a `high` or `medium` arm by a hash of its lineage's root id, kept by its retries, review fixes and continuations; the arm is in each run's note. `ttp stats --effort-ab` compares the arms (cost per landed change, output tokens and calls per run, review and checks fail rates, fix-review hops, tier-up retries) and applies the end rule (medium only with 60+ landed changes per arm and fail rates within 3 points). `effort_ab.jev_pick` (off by default) lets Jev pick medium or high per lineage from the spec, logged in the run note and Jev's call log.

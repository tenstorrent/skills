---
"tt-project": patch
---

Code arm: a live `glm_share` needs a guarded harness. On any other harness (Claude Code today) the share is
treated as 0, the task keeps its normal route and the reason is logged once per setting.

The arm's live harness is mini-swe-agent (`route.code_sonnet.glm.harness`), whose command layer is a model-free
side-effect guard: a GLM-arm run cannot push after a hand-back, note, say, ask or message anyone, write task or
database state outside its own hand-off, read stored secrets or write outside its worktree, on any harness. A
refused attempt ends the run as a hand-back to Opus. A mini run is costed from the chat bridge's token counts,
cached prompt tokens at the cache-read rate. `ttp config` and `ttp doctor` say why a live share on an unguarded
harness is treated as 0.

Policy files and digest: each project keeps its own list of policy files (`harness/policy_files.txt`, the code
behind its audit, review, approval, budget and push checks). A task that names one, or uses a policy term, stays
on Opus before the coin; a GLM-arm diff that touches one is refused and the task goes back to Opus, with no model
call either way, and a project with no list cannot run the arm live. An optional digest of large tool output
(`route.code_sonnet.glm.digest`, off by default) keeps the model's context small, with the whole output saved and
context tokens per call reported.

`ttp remove <project>` takes a project off the machine in one code path: it stops the daemon and workers,
removes the service files, drops the project's rows from the machine-wide worker and alert records and
unregisters it; files go only with `--delete-files`. The post-push deploy check follows only projects registered
on the machine, so a scratch or removed project never raises a deploy alert and an open one closes. A review's fix
task continues the task the review depends on, never one its spec only mentions.

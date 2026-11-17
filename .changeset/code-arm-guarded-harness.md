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

---
"tt-project": patch
---

The change-size rule, the PR size budget and the ponytail lane no longer refuse `ttp push`, the push queue or a review on a head whose `ttp checks` passed: each adds one follow-up task instead ("Explain or split #N" or "Fix the ponytail lane's findings on #N"), started once the change lands. The lane reads anything a user order quoted in the spec asks for as in scope. Without a pass on that head they refuse as before; the correctness review still gates landing.

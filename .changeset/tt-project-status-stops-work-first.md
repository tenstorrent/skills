---
"tt-project": patch
---

`tt-project`: `ttp status` now lists, right after the header, what needs the user, then what stops work (machine-wide holds, paused resources and providers, open login breakers, a full disk, a planned host cycle), then alerts no chat or web view read; the runtime and lease lines follow. A machine-wide hold line reads who holds it, its expected end, how to lift it, then `why: <reason>`, so a cut line loses reason text rather than the action; the web page's paused-resource reason likewise follows the action.

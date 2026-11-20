---
"tt-project": patch
---

`tt-project`: Codex's copy of tt-project now follows the installed release in more cases. A daemon's start and `ttp upgrade` check it once per release, within a time limit, and never fail on it, so a release that `ttp setup` did not refresh still gets refreshed. A git-sourced tenstorrent-skills marketplace is now refreshed with `codex plugin marketplace upgrade`, and the plugin is added only when the refreshed source serves the release; it is never moved. Shipping the runtime to a remote machine now includes the plugin manifests, so the `ttp setup` that runs there makes the release a Claude and Codex marketplace and refreshes that machine's Codex cache. A newer version folder left in the cache by an earlier registration takes one `codex plugin add`, which removes it, and later runs make no call.

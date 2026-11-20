---
"tt-project": patch
---

`tt-project`: the chat provider's logged-out pause asks its endpoint model-free again. The daemon reads `login_state`, which now runs the provider's `login_check` on the project's `providers.chat` settings, so a saved key the endpoint refuses keeps the pause with no probe run and a key it takes ends it. Tests: the dispatch duplicate guard is right; the hand-off guard tests give each run in one project its own title.

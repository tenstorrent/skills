---
"tt-project": patch
---

`tt-project`: the shell guards pass an inline Python snippet (`python3 -c '...'`, `python3 - <<'EOF'`) whose whole code a static check proves harmless: standard-library modules that run no commands and reach no network, no `eval`, `exec`, `__import__` or underscore names, and only literal file paths that are not credential stores, protected paths or system folders, writing only under the working folder or TMPDIR. The guards then judge the rest of the command as before; any other snippet keeps today's refusal. `credguard`'s working-folder scan reads each word of a long heredoc once, so a 10,000-line heredoc takes milliseconds instead of seconds.

why this size: the proof is one allowlist of AST node and call kinds (about 330 lines with its path checks), and its tests list each freed class beside the look-alikes that stay refused.

---
"tt-project": patch
---

GLM write guard: a run may write new scratch entries of its own in the system temp dir (recorded in its run folder only by the live tool hook, or the mini-swe-agent command layer, once it lets the command run: the guard checks themselves write no files, so a replay or a refused command records nothing; existing temp files it did not make, globs, variables, links out of it, device runs and the post-run scan stay outside). A `sed -i` script's `w` file is read past addresses and `s///` flags, so `s/x/wrote/` is no longer a write to `/`. Plain readers (`cat`, `grep`, `tail`, `2>&1`, for loops) may name any run's guard files; writes to them stay refused.

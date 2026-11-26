---
"tt-project": patch
---

GLM guard: fewer false refusals under the harm bar, judged at run time instead of up front. A GLM run's command
whose program name or target is a simple variable set earlier in it, or the run's own `$TMPDIR` or `$TTP_RUN_DIR`,
is run with those values written out and judged as written; credential reads, other-project and home-config writes,
protected harness files and secret printing stay refused, and a variable set conditionally, in a pipe, by `eval` or
by a sourced file still is. With the run-time Python judge loaded, the code of a plain `python3 -c "..."` call holding
an expansion (a loop's `open('$f')`) is judged as it runs, not refused up front; not when the command changes a python
setting, calls `env`, or nests a `$(...)` in that code. Outside writes: an `ls` or `stat` of a credential folder no
longer raises a high event (reading a file in it still does), and a path that is only text inside inline code that
writes only its own literal files under the run's folders is listed as mentioned, not as a write.

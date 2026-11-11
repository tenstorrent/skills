---
"tt-project": patch
---

`tt-project`:

- the host guard (`ttp checks`, the push queue, `ttp sandbox`, the plugin's test sessions) puts back a change to the real crontab or `~/.config/systemd/user` only when the run provably made it: the run's own write log names the path, its name is a test-only one (`pytest-*`, `ttp-test-*`), or the new line, unit file or link names the run's own folder (run dir, scratch TMPDIR, fake store, the test session's temp folder). Anything else, such as a unit another project installs while runs overlap, a path an open machine change names, or a removed file or line, is reported in one line naming the paths and the run and left in place: the guard never deletes what the run cannot prove it created. Before, it restored every change seen during the run, so an overlapping run could delete a unit another project had just installed on purpose; tests

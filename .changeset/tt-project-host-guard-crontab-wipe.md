---
"tt-project": patch
---

`tt-project`:

- the host guard fails a run that removed the whole real crontab (`crontab -r` by absolute path, which wipes every project's lines) and puts every line back, the harness's own included, in its old order next to any line written since. It counts as the run's only when the crontab went from lines to none during the run and no open machine change a person or project made names removing it; a single removed line (such as a series removing its own `@reboot` resume line) is still reported and left in place, and an added line is still deleted only when it names the run's own folder. Re-adding lines deletes nothing. Before, a wipe was only reported as "left in place" and the run passed; tests

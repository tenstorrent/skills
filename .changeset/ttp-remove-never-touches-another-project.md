---
"tt-project": patch
---

`ttp remove`, `ttp remove --delete-files` and `ttp stop` act only on the project whose own folder is the one named: never a parent project found by walking up from the current folder, never a folder whose harness/project.json carries another project's name, and never when the service, registry row or running daemon belongs to another project. `--delete-files` refuses a folder that holds another registered project. `ttp adopt` refuses to take over another project's name. `ttp new --dir` refuses a folder inside another project and names the scratch place, ~/.tt-project/scratch/<name>, instead.

---
"tt-project": patch
---

`tt-project`: the GLM code-arm guard lets a run write below the home folder, /tmp or /var/tmp unless the target is or holds a credential store, a protected path, the harness's own folder (but for its work folders), another registered project or a project's tt-project folder, or something another user owns (shell startup files such as ~/.bashrc and ~/.profile, and ~/.local/bin and lib, stay refused); a write as another user (sudo and the like), through a link, or to a target with a glob or expansion is judged as before. A `#` inside single quotes and a double-quoted script that expands only plain variables no longer make a read-only command look like a write.

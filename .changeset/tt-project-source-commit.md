---
"tt-project": patch
---

`tt-project`: the installed runtime records the git commit it came from.

- `ttp setup` writes the source commit (or `unknown` outside a git checkout, `-dirty` with local
  edits) into the installed runtime, and says when it replaces the same version from another commit.
- `ttp --version` prints the version and the source commit.
- `ttp upgrade` prints the harness and installed versions and commits, and reports a new commit even
  when the version is the same.

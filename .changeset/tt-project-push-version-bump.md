---
"tt-project": patch
---

`tt-project`: `ttp push` can own the version bump. With `delivery.version_bump` set (`files`,
`changeset_dir`, optional `package` and `paths`), it sets the listed files one patch version above
the branch tip after each rebase, under the push lock, and adds a changeset when the change brings
none, in one commit that a later round or rerun replaces, so parallel reviews never race for one
version. Changes outside `paths` are not bumped. With `delivery.push_wait_s` unset, a push waits for
its turn twice as long as the last passing check run plus 60 s, at least 900 s (was a fixed 300 s).

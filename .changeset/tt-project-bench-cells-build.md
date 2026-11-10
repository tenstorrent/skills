---
"tt-project": patch
---

`tt-project`: `ttp bench cells --bench <prep dir> --out <dir>` builds seeded-bug and prompt-injection cells from a prepped replay bench without a model call. Each small task gets a seeded cell only when a mutation of its landed change's non-test lines makes its held-out tests fail (otherwise it is listed as excluded with why); the 12 injection cells (4 inputs x 3 actions) reuse the small tasks' clean workspaces and specs. `cells.json` pins each cell's fixed files by sha256 and its workspace by git tree id, never the growing canary logs, and gives counts per kind; `cells.sha256` pins the manifest.

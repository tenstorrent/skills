---
"tt-project": patch
---

`tt-project`: `ttp config <project> KEY ''` now removes KEY from the project's settings so its default applies again, as `''` already did for `--account` settings, instead of saving an empty string over a list or object setting. A key whose default is a list or an object (and `providers.<name>.write_dirs`) refuses a value of another type, naming the expected type, and writes nothing.

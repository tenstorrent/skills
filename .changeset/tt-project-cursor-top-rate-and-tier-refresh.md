---
"tt-project": patch
---

`tt-project`: a Cursor run on a named model with no `pricing.cursor` row, such as a tier resolved from the CLI's model list, is now priced at a conservative top rate ($15/$1.5/$75 per million input/cached/output tokens). Before, it used the $3/$15 default, so a run on a stronger model could be estimated, and capped, below its real cost. `auto` keeps the default rate, a project's own rows still win, and cache writes are now counted as input. `ttp doctor` shows the rate each tier's spend and budget cap use. The resolved tier map is listed again once a day and right after a run whose model the CLI refused; that model stays out until the CLI or the login changes, and the refused run retries instead of failing the task. A cached `--help` answer is asked again when the CLI binary changes, so an upgrade takes effect without a daemon restart.

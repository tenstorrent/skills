---
"tt-project": patch
---

`tt-project`: fixes from review.

- The hourly runaway guard spreads a run's cost over the time it ran, so a long run that ends or
  is priced in the last hour no longer counts its whole cost as last-hour spend.
- Open questions a chat skipped because they were below its severity floor count as not
  delivered; `ttp status` and the web app say when that is why.
- `providers.claude.plugin_dirs` accepts paths that contain a comma: a JSON list, one path per
  line or separated by the OS path separator, or a single existing folder.

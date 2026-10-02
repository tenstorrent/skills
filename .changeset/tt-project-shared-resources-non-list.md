---
"tt-project": patch
---

`tt-project`: A `shared_resources` value that is not a list (for example a bare string) is now ignored
instead of being split into single characters. Slot records left by deleted project folders are ignored.

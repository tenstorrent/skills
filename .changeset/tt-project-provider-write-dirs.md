---
"tt-project": patch
---

`tt-project`: a new `providers.<name>.write_dirs` setting lists directories inside the project root (for example `notes/`) that the provider's fenced workers may write. Before, a worker writing there failed with a read-only file system error, and the only fix was turning the write fence off. Symlinks are resolved. An entry outside the project root, the root itself, or one covering the tt-project folder (charter, memory, config, state) is skipped and named in an alert. A missing directory is skipped and never created. When the setting is unset, the write roots stay as before.

---
"tt-project": patch
---

Re-running plain `ttp setup` of a force-installed older version keeps the forced-downgrade marker, so daemons no longer restore the newer release. The marker is cleared only when setup installs a different version.

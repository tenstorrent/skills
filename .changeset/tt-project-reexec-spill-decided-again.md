---
"tt-project": patch
---

A waiting `ttp checks` run that re-executes after a runtime upgrade (or a rank change) decides again whether it may spill, under the new code. Only a no-spill setting its caller handed in carries over. Before, a run the old code would not spill was kept off every spill host for good, so after an upgrade it still waited for a local slot while spill hosts sat idle. Daemon candidate prechecks run through the same path.

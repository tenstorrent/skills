---
"tt-project": patch
---

A review of a change that delivers onto an open PR's branch judges the PR's cumulative diff: the
review prompt carries the PR's whole diffstat (lines added and removed, split into code, tests, docs
and generated files) beside the repo's norm (median and p90 added lines of its recently merged PRs,
read anonymously and cached per repo for a day, `norm unknown` when the route cannot answer), and
the ponytail lane's verdict covers whether the PR as a whole still does what its title and
description say with the least code.

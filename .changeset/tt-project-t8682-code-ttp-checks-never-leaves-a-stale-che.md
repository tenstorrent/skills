---
"tt-project": patch
---

`tt-project`:

- ttp checks finish-marks checks.running however it ends, --result reads foreground runs (a dead or reused pid ends incomplete), and a hand-off waiting on checks.running is refused (upstream note 2055e4e44cd06485)
- smoke list covers the checks-running marker cases
- checks-running cases keep their own helper names

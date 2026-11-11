---
"tt-project": patch
---

`ttp checks` no longer dies before running anything when a check command has a word too long for a
file name (a pytest `-k` expression over 255 characters, as in the Mac push checks' known-failure
exclusion): the script watch treats such a word as no script instead of raising `File name too long`.

---
"tt-project": patch
---

tt-project 0.2.997: three comments no longer name a test host or another project, so the local leak-pattern test passes on a box whose pattern file lists its sibling hosts. A landing check that ran on such a box used to fail on that test alone.

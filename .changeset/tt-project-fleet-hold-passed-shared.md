---
"tt-project": patch
---

`tt-project`: when the end condition (`end_when`) of a machine-wide hold the user set passes, every project with queued tasks on that machine now shows its "needs you" alert, not only the project that checks the condition. The project that checks it writes the pass onto the hold in the shared hold file, tied to that hold's exact identity, so a hold that is set again never carries an old pass. The hold's line in `ttp machines list` also shows when its expected end passed.

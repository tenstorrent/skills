---
"tt-project": patch
---

`tt-project`: a machine-wide hold the user set reads plainly. Its line says "held until you lift it (you expected to by <time>)" and ends with "to lift it, say so in chat or use Resume on the web page", instead of a `ttp resume <project> --machine` command with a placeholder. Once its expected end has passed, each project with queued tasks on that machine shows one "needs you" alert ("<machine> is still held for you since <time>; your expected end <time> has passed; <n> of this project's tasks wait on it"). It clears when the hold is lifted or extended, or no task there waits on it, never by age. A hold that ends at the user's own end still lifts by itself.

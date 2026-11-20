---
"tt-project": patch
---

`tt-project`: the daemon no longer raises `AttributeError` every tick from the schedule 80%-of-cap warning. Its state and the over-cap timeout warning's state had shared one kv key, so a schedule's over-cap text (a string) sat where the 80% warning expected an object. They now keep separate keys. Both readers treat a stored string, list, number or null, or an entry of the wrong type, as empty, rewrite it, and record each distinct bad value once as a low issue naming the key and the type expected. Writing a non-object to either key is refused with a message naming the key. A tick step that fails with the same error every tick logs its traceback once an hour instead of each tick; the other steps still run, and failures are still counted and alerted.

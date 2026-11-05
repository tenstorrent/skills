---
"tt-project": patch
---

`tt-project`: a Restrictions charter change raises at most one tool-update task and one `restriction_tools` event, listing every flagged item and file; none when a task already covers each item. A purely additive item (a scoped `excepts` exception, or a `both_hold` item beside a standing one) leaves the older items as they were and queues no check. The tool search takes keywords only from the clauses of the item the edit changed, never from clauses it kept, the words it added or other items, and matches them as whole words only: no stems, so `rule` never matches inside `ruler` and `farm` never inside `farmer`.

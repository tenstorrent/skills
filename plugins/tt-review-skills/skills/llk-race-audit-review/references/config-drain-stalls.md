# Config rewrites and execution-unit drains

Detail for check 5 of `references/cross-invocation-state.md`.

**The rule is: drain every unit that reads the field being written — no more, no less.** Packer
config needs a stall on the packer; unpacker config needs the unpacker stalls. For math config it
depends on the field and the architecture: a field the SFPU also reads needs the SFPU drained as
well, while an FPU-only field does not.

**Do not require both math engines universally** — that is architecture- and field-specific. Getting
it wrong either way is a finding: demanding an SFPU drain for an FPU-only field flags correct code;
omitting one for a field the SFPU reads misses a real race. The in-tree reconfig paths comment on
which applies where, and they differ between architectures for documented reasons.

**A `THCON`-only stall is the classic insufficient guard** — it orders the GPR-to-config write but
drains no execution unit. Seeing a stall present is not enough; check *which* unit it drains against
which unit the config feeds.


## Why this one is easy to get wrong

It is tempting to state a single universal rule, and an earlier version of this reference did —
requiring both math engines to be drained for any math config write. That flags correct code:
where a field is read only by the FPU, draining the FPU is sufficient and the in-tree code says
so in a comment. The universal form was corrected after review. **Prefer reading the field's
consumers over applying a remembered rule.**

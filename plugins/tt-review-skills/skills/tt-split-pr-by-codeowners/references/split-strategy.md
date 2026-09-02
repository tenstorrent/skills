# Choosing the split

## The objective, and why it is this one

Minimise the **approvals each resulting PR needs to land** — not diff size, and not the reviewer
count GitHub displays.

Size is a bad proxy for review cost: what costs a reviewer is the number of independent decisions
held at once, and a 5,000-file mechanical rename is one decision while a 300-line diff changing an
API, adding a cache and fixing a bug is three. Blocking approvals are the better quantity —
computable, and each is a person who must act before the PR lands.

**Count the cover, not the union.** Owners on one rule are alternatives: a PR needs one approval per
*rule*, from anyone owning it, not one per named owner. The matcher reports both — optimise the
second. A real 174-file tt-metal PR requests 36 reviewers and is unblocked by 7 approvals; a plan
built on 36 proposes splits that save nothing.

The right number still says nothing about *coherence*, hence the rules below.

## Clustering

1. Compute the owner set per file (`scripts/codeowners_map.py`); files sharing one are the natural
   grain, and the script emits those clusters, largest first.
2. The largest cluster is usually PR 1.
3. Merge a cluster into a larger one when that adds no approver — including any it *overlaps*, since
   one shared owner covers both. Unowned files are free anywhere.
4. A cluster earns its own PR only if it forces an approver PR 1 would not need.
5. Recompute the cover after each split; stop when the next saves no approval. Two PRs needing the
   same approvals are strictly worse than one.

## Rules that override the objective

**A behaviour change and its test belong in the same slice.** Never separate them to shed an
approver. Correctness is not reviewable in a PR whose evidence landed somewhere else.

**A split that separates a change from its reason is rejected**, even when it saves an approval.
That is the failure mode of optimising on ownership: ownership cuts across concerns, so you can
produce a PR touching exactly one owner set that means nothing alone. Each PR must be reviewable as
one idea, not merely mergeable.

**Every slice compiles and passes on its own.** Where a later slice consumes something an earlier
one renames, whatever keeps the old spelling working — alias, re-export, deprecation wrapper —
belongs in the *earlier* slice, and the dependency gets stated in both. Dependencies fix merge
order, so an unstated one is a broken build waiting for someone to merge out of sequence.

**Ask about a file that straddles two clusters.** One placed wrong adds an approver.

## Wide mechanical refactors

The case that looks unsplittable: one mechanical change fanning across thousands of call sites. No
vertical slice lands green, so the usual advice fails. Sequence it as **expand / migrate /
contract**:

1. **Expand.** Add the new form beside the old so nothing breaks. The only PR in the sequence
   holding a decision, and small enough to get real scrutiny rather than being buried in noise.
2. **Migrate.** Move call sites in batches, each blocked by the expand, each green because the old
   form still exists.
3. **Contract.** Delete the old form once no caller remains, blocked by every batch.

Expand–contract never says how large a batch should be. **Owner sets answer that**: batch by owner
set, so each batch is reviewed by the people who own that code and nobody else. It falls straight
out of the clustering above.

If the API cannot be made backward compatible, price that rather than declaring it unsplittable.
Making it *temporarily* compatible is itself a deliverable, and turns "cannot be split" into "can
be, at this cost" — a decision for a human.

## When to recommend not splitting

A confident **do not split** is a first-class output. Say it plainly when:

- one or two approvals already cover every file — there is nothing to win;
- the change is one decision spread wide, so splitting yields N PRs that must land together: N
  review contexts, none independently correct;
- the shim it would need costs more than the latency it saves;
- the base branch does not enforce code-owner review (check both mechanisms — see
  `references/codeowners-semantics.md`).

State the price: N× CI, N× review latency, a rebase chain — and note that a branch's
required-approval count applies to *every* slice, so N slices cost N× that floor however cleanly
ownership divides. If the total exceeds the saving, argue against your own proposal.

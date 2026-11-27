# Vision validation gates

## Required assets

If a required source, checkpoint or dataset is absent, check existing copies, authoritative
access terms, download size and storage. Download obtainable assets into ignored artifacts;
verify and record provenance. Record the exact access failure when retrieval fails. Never commit
downloaded assets or silently shrink the contracted metric.

## Top-k selection

Run selectors independently, retain upstream indices and align downstream tensors by shared
index before PCC; never feed reference indices into TTNN acceptance runs. Report both selected
counts, shared and unmatched counts. Record the reference score gap between ranks k and k+1, and
each unmatched candidate's reference-score distance from the rank-k score. Every unmatched
candidate must lie within the measured upstream score error of that boundary; one outside it is a
selector or upstream defect, not precision noise. Do not raise model precision only to make
selections identical; boundary swaps within score error are expected and are judged by the task
metric. Aligned PCC alone is insufficient: run the task metric and check rank order when the
declared output requires it.

## Task evaluation

- Estimate remaining total time across configurations and reference/device passes from a bounded
  pilot or warmed timings, including startup, completed samples and uncertainty.
- If it exceeds one hour, warn the user before the long run. Offer the full contracted check and
  feasible faster choices with an ETA and acceptance consequence for each. A fixed subset gives
  an early signal, not a full-dataset pass; final use requires an explicit contract, protocol and
  threshold revision plus review.
- Reuse predictions or a faster path only after verifying identical samples and metric semantics.
  Do not promise savings from an unverified path or a pilot followed by the full run.
- Await a choice unless already authorized. Without a runner reply path, report the pending
  decision and leave the long check unstarted. On an authorized resume, preserve work and update
  the ETA without asking again.

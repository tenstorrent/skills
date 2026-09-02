# Scope and evidence

## Reading past the diff

A diff shows changed lines, not consequences. Five moves, in rough order of value:

1. **Read the whole file.** Structure, naming, and style are judged against the file, not the hunk.
   Does the addition belong here? Does it duplicate something above it?
2. **Grep for the pattern.** New helper, constant, or utility? Search first. The canonical version
   usually exists already, and reinvention is a finding in its own right.
3. **Trace callers and callees.** A changed signature has call sites outside the diff. A new call
   site has behaviour defined at the callee. Both need reading before you can judge the change.
4. **Find a neighbour.** A sibling ttnn op, a parallel kernel, a comparable test. Structural fit is
   relative to neighbours.
5. **Validate intent.** What is this trying to do? Does the code do that? Does the test test *that*
   rather than something adjacent?

## What counts as evidence

Acceptable:

- A path in the repo under review, with a line number.
- A documented invariant, cited by the file that states it.
- A reference file shipped with the skill raising the finding.
- A reproduction: the specific input, shape, or configuration that triggers the problem.

Not acceptable:

- "This is a common pattern."
- "This may cause issues."
- Model recall about an API's behaviour, unverified against the tree under review.

## Ground or abstain

When a finding depends on a fact you cannot verify from the repo under review or from a shipped
reference file, you have exactly two honest options:

- **Ground it** — find the fact in the tree, cite it, and report normally.
- **Abstain** — state the unresolved question in the finding, downgrade one severity step, and list
  it under `## Unresolved`.

Fabricating the fact is the one unacceptable option. This matters most where ground truth lives in
sources unavailable to CI: internal documentation, hardware specifications, unreleased
architectures. For those, abstain. A review that says "I could not verify X, so this is CONSIDER
rather than MUST-FIX" is trustworthy. One that invents X is not, and one bad invented finding costs
more trust than ten good findings earn.

## Severity downgrade, worked

> **[SHOULD-FIX]** Semaphore may not be reset between iterations
> - File: `foo/bar_kernel.cpp:212`
> - Issue: The semaphore is set on line 204 but I could not locate a reset on the loop back-edge.
>   Downgraded from MUST-FIX: the reset may happen in the caller, which is outside this diff and I
>   could not identify it.
> - Suggestion: Confirm the reset path; if it is in the caller, a comment at the set site would make
>   the contract legible.

That is the shape. The uncertainty is stated, the severity reflects it, and the reader can resolve
it in one step.

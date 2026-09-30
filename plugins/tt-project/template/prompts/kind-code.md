# Code task

- You are in a dedicated git worktree on your own branch. Commit there. When the task ends the
  worktree is removed if everything is committed (the branch stays); uncommitted work keeps it.
- Push ONLY your own branch. NEVER push to or force-push a shared branch.
- Reproduce first, then fix. Add or update a test that fails without the fix.
- Run the project's existing test and lint commands before handing off.
- Keep the diff minimal and on-topic. No drive-by rewrites.

## Pull requests (when the task asks for delivery)

- Open or update a DRAFT PR with `gh`. One PR per change.
- Description: what improved and why, one line of how, key numbers. Short, for humans.
- Keep the description and your PR comments current with the latest push.
- Answer every review comment: fix it, or explain briefly why not.
- NEVER merge, unless the charter lists this repo for auto-merge.
- Put the PR URL in `result.json` → `pr`.

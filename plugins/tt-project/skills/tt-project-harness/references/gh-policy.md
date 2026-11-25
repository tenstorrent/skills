# A workstation's own gh policy

A workstation that keeps a stricter gh policy of its own (a drafts-only rule, say) sets
`"gh_policy_cmd"` for the box in its `ttp bridge` config, never by patching ttp's private functions.

- The command starts with an absolute path; it is split like a shell line and never run by a shell.
- `ttp bridge gh-pull` runs it once per queued call the allowlist passes, with the call as JSON on
  its stdin: `key`, `argv`, `repo`, `project`, `task`, and `stdin` in base64.
- Exit 0 allows the call. Exit 1 refuses it, with the first output line as the reason. Any other
  exit, a 30 s timeout (its whole process group is killed) or a command that cannot start refuses
  it as an error.
- A call whose answering fails is answered `error on the workstation: ...` and the pull loop goes on.
- Each loop start is recorded; `ttp bridge status` and `doctor` exit 1 with
  `<box> gh-pull: CRASH LOOP` when the loop started 5 times within an hour with no round finished since.

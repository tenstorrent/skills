---
"tt-project": patch
---

`tt-project`:

- ttp bench prep: validate held-out tests fail-to-pass (base fails, head passes 3 of 3), hold out every changed test file, follow dotted imports, scan only held-out tests; faster scan (carrier reach, memoized git reads, one parse per file version, packed workspaces)
- ttp bench prep: --test-cmd (one plain pytest run) and --no-validate
- bench docs and changeset: validation, --test-cmd, --no-validate
- ttp bench prep: parse large test files piece by piece, each top-level piece cached by its text (a new version parses only what it changed)
- ttp bench prep: build kept workspaces in a pool beside the scan; changeset names what made the scan faster
- ttp bench: mark the validation test run's own session (killed whole past its timeout)
- ttp bench prep: keep the push branch's parent-branch replay (PARENT, from_parent) and shared seal() beside validation, carrier reach and packed workspaces; benchrun.audit calls bench.quotes with a line list; the parent-branch test's kid is a real fix so it is fail-to-pass
- rebase onto relay/smartonTT/tt-project: bench_cases.py and operate.md keep the push branch's newer lines (cellenv OS-site test, python3 test-cmd, model-bench, memory and config rows, tick-step and reap docs) beside the bench prep changes
- ttp bench prep: carrier reach unpacks the push branch's sixth link field (follows) and walks it, so tasks joined by follows labels keep their landing

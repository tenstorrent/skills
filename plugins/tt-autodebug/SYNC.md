# Prompt development and synchronization

The installed `tt-autodebug` plugin is a self-contained, reviewed snapshot. Prompt development and
backtesting currently continue on the `main` branch of the maintainer-local `autodebug` Git
repository. The installed plugin never reads that checkout at runtime.

[`sync-source.json`](sync-source.json) records the exact source commit, Git blob, destination, and
SHA-256 digest for every imported prompt. This makes a manual import reviewable now and gives a
future synchronization tool a stable contract.

## Manual update

1. Finish and commit the prompt change on `main` in the standalone `autodebug` repository. It is
   fine for unrelated result files or cases to be in progress, but both imported prompt files must
   match the recorded source commit.
2. Create a branch in `tenstorrent/skills`. Copy only the prompt files listed in
   `sync-source.json` to their listed destinations. Do not import backtest cases, generated reports,
   credentials, machine configuration, or watcher state.
3. Update the source commit, Git blob IDs, SHA-256 digests, and import date in
   `sync-source.json`.
4. Review the prompt diff semantically. Preserve the `{{PROBLEM}}` and
   `{{FOCUS_PATH_SECTION}}` placeholders and the `AUTODEBUG.md` or `AUTOTRIAGE.md` output contract.
5. Update both `tt-autodebug` host-manifest versions together and add a changeset that explains the
   user-visible prompt change.
6. Run the repository validation suite and both plugin validators. Record any relevant standalone
   backtest result in the PR, but do not copy generated backtest output into this plugin.

## Future automation contract

A future sync agent or script should:

- accept the standalone checkout through an explicit `--source` argument; never embed a
  maintainer-specific absolute path;
- default to a read-only `--check` mode and require an explicit update mode before writing;
- refuse an update when an imported source prompt differs from the recorded source commit;
- copy only the allowlisted file mappings in `sync-source.json`;
- update and verify every recorded digest; and
- leave version bumps, changeset text, semantic review, and backtest interpretation visible for
  human review.

Until that tool exists, the process is intentionally manual. A newer local prompt is not
automatically the published prompt.

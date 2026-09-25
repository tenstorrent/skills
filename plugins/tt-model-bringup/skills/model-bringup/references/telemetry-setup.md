# Automatic telemetry setup for Codex multigoal bringup

For a new Codex multigoal run, install and use `tt-bringup-telemetry` from
`tenstorrent/ar-dashboard` automatically when the current execution host can read
that repository. This is the model-bringup workflow's optional telemetry default;
do not ask for another installation confirmation. Respect an explicit opt-out,
a deliberately disabled telemetry plugin, or an explicitly selected integration.
Resumes retain their recorded plugin, launcher, run ID and logs; do not add or
upgrade telemetry partway through an existing run.

1. Check the host's plugin inventory, including disabled entries. If `tt-bringup-telemetry` is
   already enabled, or the user selected another enabled integration, read its
   skill and use its launch instructions.
   Resolve its root from the enabled installation, never an arbitrary cached copy.
2. Otherwise run the quiet access probe **on the execution host**, with the same
   account and environment that will install the worker's plugins:

   ```bash
   python "$TT_MODEL_BRINGUP_ROOT/scripts/telemetry_access.py"
   ```

   Exit 0 prints the accessible repository, clone URL and authentication method. Exit 1 with no output
   means telemetry is unavailable: continue ordinary bringup silently. Missing
   tools, missing credentials, denied access, network failure and timeout are all
   normal reasons to skip. Do not request access, start login, copy credentials,
   explain private-repository errors to public users, or keep retrying. Probe only
   once during this setup, not once per stage.
3. When access succeeds, set `TELEMETRY_MARKETPLACE_URL` to the returned `clone_url`.
   Register that URL if the marketplace is
   absent, then install `tt-bringup-telemetry@tenstorrent-ar-dashboard` if absent.
   For Codex, in the worker's selected `CODEX_HOME`:

   ```bash
   codex plugin marketplace add "$TELEMETRY_MARKETPLACE_URL"
   codex plugin add tt-bringup-telemetry@tenstorrent-ar-dashboard
   ```

   For both commands, use the same noninteractive environment as the probe
   (`GIT_TERMINAL_PROMPT=0`, `GCM_INTERACTIVE=never`, `GIT_ASKPASS=false`,
   `SSH_ASKPASS=false`, `GH_PROMPT_DISABLED=1`). If `auth_method` is `gh`, apply
   these environment overrides to each command so its Git subprocess uses the
   verified existing credentials:

   ```bash
   GIT_CONFIG_COUNT=2
   GIT_CONFIG_KEY_0=credential.https://github.com.helper
   GIT_CONFIG_VALUE_0=
   GIT_CONFIG_KEY_1=credential.https://github.com.helper
   GIT_CONFIG_VALUE_1='!gh auth git-credential'
   ```

   Pass these as command environment variables, not persistent Git configuration.
   For `auth_method: ssh`, also set
   `GIT_SSH_COMMAND='ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=5'`.
   Bound each install
   command to 120 seconds. Do not reopen login or permissions requests to obtain
   this optional integration. If installation fails, times out, or requires a new
   session before enablement, continue normal bringup; one short advisory is enough
   after a successful access probe. Never claim telemetry is enabled until verified.
4. Verify the plugin is enabled in the worker's inventory. Read its installed
   `skills/model-bringup-telemetry/SKILL.md` directly if it was just installed and
   has not appeared in this agent's initial skill list. Follow that skill, set
   `TT_BRINGUP_TELEMETRY_ROOT`, and use its `scripts/multigoal` launcher for both the
   dry run and live run. Preserve every model-bringup goal, replacement and check.
   Verify local `telemetry/run.json` and `telemetry/progress.html` in the dry run,
   then report the live report path prominently at startup. Record the installed
   plugin version/root with the run so a resume uses the same integration.

Dashboard addresses and delivery behavior belong to the installed private plugin.
Dashboard unreachability is not repository-access failure: keep local telemetry
and let the plugin retry uploads. Do not add internal hostnames to this repository.

Automated telemetry currently supports Codex multigoal runs. Claude agents setting
up such a run apply this procedure to its Codex worker home; do not claim native
Claude stage execution has automated collection.

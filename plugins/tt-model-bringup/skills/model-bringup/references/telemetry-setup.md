# Automatic telemetry setup for Codex multigoal bringup

For a new Codex multigoal run, install and use `tt-bringup-telemetry` from
`tenstorrent/ar-dashboard` automatically when the current execution host can read
that repository. This is the model-bringup workflow's optional telemetry default;
do not ask for another installation confirmation. Respect an explicit opt-out,
a deliberately disabled telemetry plugin, or an explicitly selected integration.
For a resume, skip automatic discovery and installation. Replay the recorded
launcher, plugin root/version, telemetry arguments and environment with the same
log directory. The runner does **not** persist or restore the telemetry selection;
`--resume-stage` alone does not preserve it. Verify the recorded selection against
the installed plugin before launching. Recover missing setup details from the
run's existing artifacts or launch wrapper. If the original integration cannot be
verified, continue bringup without telemetry and report that limitation briefly;
do not silently substitute or upgrade to the current default plugin.

1. Check the host's plugin inventory, including disabled entries, in this order:
   - On an explicit telemetry opt-out, end telemetry setup here and continue ordinary
     bringup. Do not probe, install, enable or invoke telemetry.
   - If the user selected another enabled integration, read and follow its skill;
     do not discover or install the default integration.
   - If `tt-bringup-telemetry` is present but disabled, end telemetry setup here and
     continue ordinary bringup. Do not probe, reinstall or enable it automatically.
     A new explicit instruction to enable it is required to change this choice.
   - If it is enabled, read its skill and use its launch instructions.
   Resolve roots from enabled installations, never arbitrary cached copies.
2. Only when no integration was selected and `tt-bringup-telemetry` is absent,
   run the quiet access probe **on the execution host**, with the same
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
3. When access succeeds, set `TELEMETRY_MARKETPLACE_URL` and
   `TELEMETRY_AUTH_METHOD` from the returned `clone_url` and `auth_method`.
   In the worker's selected `CODEX_HOME`, register that URL if the marketplace is
   absent, then install `tt-bringup-telemetry@tenstorrent-ar-dashboard` if absent.
   Use the verified credentials with exported, noninteractive settings. This
   subshell scopes them to installation and leaves persistent Git configuration
   unchanged (run only the add commands needed for the current inventory):

   ```bash
   (
     export GIT_TERMINAL_PROMPT=0 GCM_INTERACTIVE=never GIT_ASKPASS=false
     export SSH_ASKPASS=false GH_PROMPT_DISABLED=1
     if [ "$TELEMETRY_AUTH_METHOD" = gh ]; then
       export GIT_CONFIG_COUNT=2
       export GIT_CONFIG_KEY_0=credential.https://github.com.helper
       export GIT_CONFIG_VALUE_0=
       export GIT_CONFIG_KEY_1=credential.https://github.com.helper
       export GIT_CONFIG_VALUE_1='!gh auth git-credential'
     elif [ "$TELEMETRY_AUTH_METHOD" = ssh ]; then
       export GIT_SSH_COMMAND='ssh -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=5'
     fi
     codex plugin marketplace add "$TELEMETRY_MARKETPLACE_URL"
     codex plugin add tt-bringup-telemetry@tenstorrent-ar-dashboard
   )
   ```

   Bound each install command to 120 seconds using the execution tool's timeout.
   Do not reopen login or permissions requests to obtain this optional integration.
   If installation fails, times out, or requires a new session before enablement,
   continue normal bringup; one short advisory is enough after a successful access
   probe. Never claim telemetry is enabled until verified.
4. Verify the plugin is enabled in the worker's inventory. Read its installed
   `skills/model-bringup-telemetry/SKILL.md` directly if it was just installed and
   has not appeared in this agent's initial skill list. Follow that skill, set
   `TT_BRINGUP_TELEMETRY_ROOT`, and use its `scripts/multigoal` launcher for both the
   dry run and live run. Preserve every model-bringup goal, replacement and check.
   Verify local `telemetry/run.json` and `telemetry/progress.html` in the dry run,
   then report the live report path prominently at startup. Before live launch,
   save the exact launcher command, plugin root/version, telemetry arguments and
   environment alongside the run's setup notes or launch wrapper. Record disabled
   telemetry explicitly too. On resume, read and reuse that record; do not assume
   `manifest.txt` restores it or substitute the current default plugin.

Dashboard addresses and delivery behavior belong to the installed private plugin.
Dashboard unreachability is not repository-access failure: keep local telemetry
and let the plugin retry uploads. Do not add internal hostnames to this repository.

Automated telemetry currently supports Codex multigoal runs. Claude agents setting
up such a run apply this procedure to its Codex worker home; do not claim native
Claude stage execution has automated collection.

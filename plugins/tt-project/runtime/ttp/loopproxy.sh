# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
# Sourced in every Bash command of a Claude Code worker (CLAUDE_ENV_FILE, set by loopproxy.py): sources an
# inherited CLAUDE_ENV_FILE, then puts the harness bin (TTP_RUN_BIN) first on PATH again. Plain sh syntax:
# the user's shell may be bash or zsh.
if [ -n "${TTP_PREV_CLAUDE_ENV_FILE-}" ] && [ -r "$TTP_PREV_CLAUDE_ENV_FILE" ]; then
  # Unset while it runs, so a previous file that is a copy of this script ends after one pass.
  _ttp_p=$TTP_PREV_CLAUDE_ENV_FILE
  unset TTP_PREV_CLAUDE_ENV_FILE
  . "$_ttp_p"
  TTP_PREV_CLAUDE_ENV_FILE=$_ttp_p; export TTP_PREV_CLAUDE_ENV_FILE
  unset _ttp_p
fi
# The harness gh wrapper's folder first on PATH again, whatever the user's shell setup put in front of it
# (ghhold.py): a bare `gh` stays the wrapper.
if [ -n "${TTP_RUN_BIN-}" ]; then
  case ":$PATH" in ":$TTP_RUN_BIN:"*) ;; *) PATH="$TTP_RUN_BIN:$PATH"; export PATH ;; esac
fi

# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
# Sourced in every Bash command of a Claude Code worker (CLAUDE_ENV_FILE, set by loopproxy.py): a proxy
# variable whose host is a literal localhost gets 127.0.0.1, which resolves inside the Bash sandbox.
# The scheme, credentials, port and path stay. Plain sh syntax: the user's shell may be bash or zsh.
for _ttp_v in HTTPS_PROXY https_proxy HTTP_PROXY http_proxy ALL_PROXY all_proxy FTP_PROXY ftp_proxy; do
  eval "_ttp_x=\${$_ttp_v-}"
  case $_ttp_x in *localhost*) ;; *) continue ;; esac
  case $_ttp_x in *://*) _ttp_s="${_ttp_x%%://*}://"; _ttp_r="${_ttp_x#*://}" ;; *) _ttp_s=; _ttp_r=$_ttp_x ;; esac
  _ttp_a="${_ttp_r%%/*}"
  case $_ttp_a in *@*) _ttp_u="${_ttp_a%@*}@" ;; *) _ttp_u= ;; esac
  _ttp_h="${_ttp_r#"$_ttp_u"}"
  case $_ttp_h in localhost|localhost:*|localhost/*) export "$_ttp_v=${_ttp_s}${_ttp_u}127.0.0.1${_ttp_h#localhost}" ;; esac
done
if [ -n "${TTP_PREV_CLAUDE_ENV_FILE-}" ] && [ -r "$TTP_PREV_CLAUDE_ENV_FILE" ]; then
  . "$TTP_PREV_CLAUDE_ENV_FILE"
fi
unset _ttp_v _ttp_x _ttp_s _ttp_r _ttp_a _ttp_u _ttp_h

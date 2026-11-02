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
# Host loopback ports (loopproxy.ports_env): in the Bash sandbox's own network namespace, a listener on
# 127.0.0.1:<port> per port that reaches localhost:<port> on the host through the sandbox's proxy. Only
# from a namespace other than the worker's own (TTP_HOST_NETNS), and only on a port free there, so a
# command outside the sandbox binds nothing. The listeners end with the command's namespace.
if [ -n "${TTP_LOOPBACK_PORTS-}" ] && [ -n "${TTP_HOST_NETNS-}" ] && [ -r /proc/net/tcp ] \
   && [ "$(readlink /proc/self/ns/net 2>/dev/null)" != "$TTP_HOST_NETNS" ] && command -v socat >/dev/null 2>&1; then
  _ttp_r="${HTTPS_PROXY:-${HTTP_PROXY-}}"
  _ttp_r="${_ttp_r#*://}"; _ttp_r="${_ttp_r%%/*}"
  case $_ttp_r in *@*) _ttp_u="${_ttp_r%@*}"; _ttp_r="${_ttp_r##*@}" ;; *) _ttp_u= ;; esac
  case $_ttp_r in 127.0.0.1:*) _ttp_h="${_ttp_r#127.0.0.1:}" ;; *) _ttp_h= ;; esac
  case $_ttp_h in ''|*[!0-9]*) _ttp_h= ;; esac
  # Ports below this one need a privilege the sandbox's user lacks (1024 unless the host lowered it).
  _ttp_s=$(cat /proc/sys/net/ipv4/ip_unprivileged_port_start 2>/dev/null)
  case $_ttp_s in ''|*[!0-9]*) _ttp_s=1024 ;; esac
  _ttp_x=
  _ttp_r=$(cat /proc/net/tcp 2>/dev/null)
  _ttp_l="${_ttp_h:+$TTP_LOOPBACK_PORTS }"   # sliced by hand: zsh does not split an unquoted variable
  while [ -n "$_ttp_l" ]; do
    _ttp_v="${_ttp_l%% *}"; _ttp_l="${_ttp_l#* }"
    case $_ttp_v in ''|*[!0-9]*) continue ;; esac
    [ "$_ttp_v" -ge "$_ttp_s" ] || continue
    _ttp_a=$(printf '%04X' "$_ttp_v")
    case $_ttp_r in *":$_ttp_a 00000000:0000 0A"*) continue ;; esac   # taken here (a proxy bridge)
    socat "TCP-LISTEN:$_ttp_v,bind=127.0.0.1,fork,reuseaddr" \
      "PROXY:127.0.0.1:localhost:$_ttp_v,proxyport=$_ttp_h${_ttp_u:+,proxyauth=$_ttp_u}" </dev/null >/dev/null 2>&1 &
    _ttp_x="$_ttp_x$_ttp_a "
  done
  # Wait (about half a second at most) until every listener is up, so the command's first connect finds it.
  _ttp_s=0
  while [ -n "$_ttp_x" ] && [ "$_ttp_s" -lt 50 ]; do
    _ttp_r=$(cat /proc/net/tcp 2>/dev/null); _ttp_h=; _ttp_l=$_ttp_x
    while [ -n "$_ttp_l" ]; do
      _ttp_a="${_ttp_l%% *}"; _ttp_l="${_ttp_l#* }"
      case $_ttp_r in *":$_ttp_a 00000000:0000 0A"*) ;; *) _ttp_h="$_ttp_h$_ttp_a " ;; esac
    done
    _ttp_x=$_ttp_h; _ttp_s=$((_ttp_s + 1))
    [ -n "$_ttp_x" ] && sleep 0.01
  done
fi
if [ -n "${TTP_PREV_CLAUDE_ENV_FILE-}" ] && [ -r "$TTP_PREV_CLAUDE_ENV_FILE" ]; then
  . "$TTP_PREV_CLAUDE_ENV_FILE"
fi
unset _ttp_v _ttp_x _ttp_s _ttp_r _ttp_a _ttp_u _ttp_h _ttp_l

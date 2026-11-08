# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Cursor CLI (`agent -p`, also installed as `cursor-agent`). No cost field, no budget flag and no
effort flag: reasoning depth is part of the model name, so tiers map to model names. Usage tokens
are read when the build reports them; cost is estimated from the project's price table.

Builds that offer `--output-format stream-json` stream one event per line, ending with the same
result object `json` prints alone. Streaming keeps the output file growing while the agent works,
so the runner's stall guard sees progress and its budget check sees spend before the run ends.

Write fence (from the docs and `agent --help`; built, not measured live). Workers run with `--force`.
When `--help` lists `--sandbox` and `--add-dir`, a flag can open the sandbox's network
(`sandbox_network_gap()`; none does yet, so only projects under the no_internet restriction, which
want the network blocked, pass this) and the host passes `sandbox_host_gap()`, they also get `--sandbox enabled` and one `--add-dir` per writable root
(`writable_args`: the project state with the run dir, git metadata, temp and lock dirs), so
result.json stays writable without the harness writing `.cursor/sandbox.json`. Otherwise they are
unfenced and `write_fence()` says why.

| Question | Linux | macOS |
| :- | :- | :- |
| Fence writes to a list of dirs | yes with `--sandbox enabled`: Landlock + seccomp, kernel 6.2+ with unprivileged user namespaces; extra dirs in `.cursor/sandbox.json` `additionalReadwritePaths` | yes, Seatbelt |
| Unix socket under `state/` from the sandbox | not documented | not documented |

Harness hook and credential reads: a worker or reviewer run gets the harness hook through the
project hooks file Cursor loads from its workspace (`.cursor/hooks.json`; no CLI flag or environment
variable sets hooks for one run). prepare_run merges the hook's entries into that file for the run
(ttp.cursorhook: install, undo at run end), so the Bash guards, live updates and the Stop hand-off
guard match a Claude worker's. `denied_reads` reaches that hook as TTP_DENIED_READS, refused for the
Read and Grep tools; shell reads go through the hook's own credential guard."""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from pathlib import Path

from .. import cursorhook
from . import register
from .base import (_CLI_OUTPUT, AUTH_RE, CMD_MAX, LIMIT_RE, Provider, RunUsage, cli_output, head80, input_hash,
                   jsonl_from, price_row, request_rejected, status_check, stderr_tail)

PRICES = {"default": (3.0, 0.3, 15.0)}
# A named model with no pricing.cursor row (a tier resolved from the CLI's own list, cursortiers.py,
# can be the strongest reasoning model listed) is priced at TOP, the top published list rate of the
# frontier models Cursor offers, so its estimate and budget cap never come in under the real cost.
# `auto` (Cursor's router) keeps "default". A project's own rows, its "default" one included, win.
TOP = (15.0, 1.5, 75.0)
FALLBACK_MODEL = "auto"
USAGE_KEYS = ("inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens")
# account(): `agent status` is asked again only this often, or at once when the login files change.
ACCOUNT_TTL_S = 600.0
_ACCOUNT: dict[str, tuple[float, str]] = {}   # binary|credentials stamp -> (asked at, label)
_LAST_LABEL: dict[str, str] = {}              # binary -> the last login label seen
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
LOGIN_RE = re.compile(r"(?:logged in|signed in|authenticated)\s+as\s+[\"'`]?([^\s\"'`]+)|"
                      r"\b(?:user|email|account)\s*:\s*[\"'`]?([^\s\"'`]+)", re.I)
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
LOGGED_OUT_RE = re.compile(r"not (logged|authenticated|signed)|login required|unauthenticated", re.I)


def rate(model: str, prices: dict | None = None) -> tuple[tuple[float, float, float], str]:
    """($ per million input, cached input, output tokens) for `model`, and where that rate comes from."""
    rows = {k for k, v in (prices or {}).items() if isinstance(v, (list, tuple)) and len(v) == 3}
    if model in rows:
        return price_row(PRICES, prices or {}, model), f"pricing.cursor.{model}"
    if not model or model in (FALLBACK_MODEL, *PRICES) or "default" in rows:
        return price_row(PRICES, prices or {}, "default"), \
            ("pricing.cursor.default" if "default" in rows else "the shipped default rate")
    return TOP, "the top rate: no pricing.cursor row for this model"


def status_login(text: str) -> str:
    """The login `agent status` names (an email or user name), "" when it names none or says logged out."""
    text = ANSI_RE.sub("", text or "")
    if LOGGED_OUT_RE.search(text):
        return ""
    m = LOGIN_RE.search(text)
    who = next((g for g in (m.groups() if m else ()) if g), "") or (EMAIL_RE.search(text) or [""])[0]
    return who.strip(".,;:()[]").lower()


def sandbox_host_gap(system: str = "", release: str = "", proc: str = "/proc/sys/kernel") -> str:
    """Why this host cannot run Cursor's sandbox; "" when it can. macOS: Seatbelt, always. Linux:
    Landlock + seccomp need kernel 6.2+ and unprivileged user namespaces (an AppArmor or Debian
    switch may forbid them)."""
    u = os.uname()
    system, release = system or u.sysname, release or u.release
    if system == "Darwin":
        return ""
    if system != "Linux":
        return f"no Cursor sandbox on {system}"
    m = re.match(r"(\d+)\.(\d+)", release)
    if not m or (int(m[1]), int(m[2])) < (6, 2):
        return f"kernel {release} is older than the 6.2 the Cursor sandbox needs"
    for name, allowed in (("apparmor_restrict_unprivileged_userns", "0"), ("unprivileged_userns_clone", "1")):
        try:
            value = Path(proc, name).read_text().strip()
        except OSError:
            continue   # absent: the switch does not exist on this kernel
        if value != allowed:
            return f"unprivileged user namespaces are off (kernel.{name}={value})"
    return ""


def sandbox_network_gap(help_text: str) -> str:
    """Why `--sandbox enabled` would cut workers off the network; "" once a flag can open it. Build
    2026.10.22-27dcf5f (read from its bundled code, not run live): the sandbox's network policy is
    deny by default, plus a server-sent allowlist; only `sandbox.networkAccess` in the CLI config or
    an allow list in `.cursor/sandbox.json` opens it, and no flag in `--help` does. git fetch, gh and
    ssh must work in a worker, and the harness writes neither file."""
    return "Cursor's sandbox denies network by default and no CLI flag allows it (git, gh and ssh would fail)"


def account_label(who: str) -> str:
    """The account label for login `who`: 'cursor:' and an opaque hash, never the login itself."""
    return f"cursor:{hashlib.sha256(who.encode()).hexdigest()[:12]}" if who else "cursor"


@register
class Cursor(Provider):
    name = "cursor"
    # `--resume <chatId>`, once session_saved finds the chat's store (a resume of a lost chat may start an
    # empty one). The session's size is the run's summed usage (no per-call usage): it compacts early, never late.
    coord_session = "agent"
    # 2026.10.22: `--resume [chatId]` and `create-chat`, no fork; a copy of a chat's store.db would rest
    # on an undocumented layout, so its runs take the disk block (ctxfork.py) and only a lost run resumes.
    no_fork_why = "cursor-agent resumes its own chats (--resume) but has no fork, and its chat store is undocumented"
    binaries = ("agent", "cursor-agent")
    api_host = "api2.cursor.sh"
    login_hint = "run `agent login` there"
    isolate_read_only = True

    def credential_files(self) -> list[str]:
        # `agent login` writes auth.json under $XDG_CONFIG_HOME/cursor (default ~/.config/cursor) on
        # Linux; macOS keeps tokens in the Keychain, where the missing file just never changes.
        return [str(Path(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")) / "cursor" / "auth.json")]

    def login_check(self) -> bool | None:
        b = self.binary()
        if not b or not re.search(r"^\s*status\b", cli_output(b, "--help"), re.M):
            return None   # a CLI without `agent status`
        got = status_check([b, "status"])
        if got is None:
            return None
        return got[0] == 0 and not LOGGED_OUT_RE.search(got[1])

    def build(self, *, role, model, effort, cwd, budget_usd, read_only, schema, restrictions,
              disallowed_tools=(), denied_reads=(), web=True):
        exe = self.binary() or "agent"
        help_text = cli_output(exe, "--help")
        fmt = "stream-json" if "stream-json" in help_text else "json"
        argv = [exe, "-p", "--output-format", fmt, "--trust", "--workspace", cwd]
        if model:
            argv += ["--model", model]
        if not read_only:
            argv += ["--force"]
        elif re.search(r"--mode\b[\s\S]{0,400}?\bask\b", help_text):   # its choices may wrap
            argv += ["--mode", "ask"]   # answers only: no edits and no commands
        return argv, ({cursorhook.DENIED_READS_ENV: os.pathsep.join(map(str, denied_reads))} if denied_reads else {})

    def prepare_run(self, cwd: str, run_dir: Path) -> str:
        return cursorhook.install(cwd, str(run_dir))

    def _sandbox_gap(self, restrictions: dict | None = None) -> str:
        """Why workers run without `--sandbox enabled`; "" when they get it. Under the no_internet
        restriction the sandbox's blocked network is wanted, so only the flags and host count."""
        help_text = cli_output(self.binary() or "agent", "--help")
        missing = [f for f in ("--sandbox", "--add-dir") if not re.search(rf"(?<![\w-]){f}\b", help_text)]
        if missing:
            return f"--force without --sandbox enabled: this build's `--help` lists no {' or '.join(missing)}"
        no_internet = bool((restrictions or {}).get("no_internet"))
        net = "" if no_internet else sandbox_network_gap(help_text)
        if net:
            return f"--force without --sandbox enabled: {net}; only projects under no_internet get it"
        gap = sandbox_host_gap()
        return f"--force without --sandbox enabled{' under no_internet' if no_internet else ''}: {gap}" if gap else ""

    def writable_args(self, dirs, restrictions=None):
        # `--add-dir` makes each root a workspace root, writable inside the sandbox; the harness never
        # writes `.cursor/sandbox.json` (`additionalReadwritePaths`) for this.
        if not dirs or self._sandbox_gap(restrictions):
            return []
        out = ["--sandbox", "enabled"]
        for d in dirs:
            out += ["--add-dir", str(d)]
        return out

    def launch_sandbox(self, argv):
        i = argv.index("--sandbox") if "--sandbox" in argv else None
        on = i is not None and argv[i + 1:i + 2] == ["enabled"]
        return super().launch_sandbox(argv) or ("cursor-sandbox" if on else "")

    def write_fence(self, restrictions=None) -> str:
        return self._sandbox_gap(restrictions)

    def coord_tool_set(self, cwd):
        # A coordinator turn is read-only (build: `--mode ask` and no `--force`); no per-call hooks.
        return "read", "Cursor runs coordinator wakes in its ask mode: it reads files but runs no commands and edits nothing"

    def resume_args(self, session_id: str) -> list[str]:
        # `--resume [chatId]` continues a chat; the id is the session_id every stream event carries.
        ok = session_id and re.fullmatch(r"[A-Za-z0-9-]+", session_id)
        return ["--resume", session_id] if ok and "--resume" in cli_output(self.binary() or "agent", "--help") else []

    def session_saved(self, session_id: str, cwd: str, env: dict | None = None) -> bool:
        # The CLI keeps each chat in <config>/chats/<md5 of the resolved cwd>/<chatId>/store.db, its config
        # CURSOR_CONFIG_DIR, else $XDG_CONFIG_HOME/cursor, else ~/.cursor (read from cursor-agent
        # 2026.10.22's bundle; it loads a chat only when that store.db exists). Another cwd spelling: any folder.
        if not session_id or not re.fullmatch(r"[A-Za-z0-9-]+", session_id):
            return False
        bases = []
        for e in (env or {}, os.environ):
            if (e.get("CURSOR_CONFIG_DIR") or "").strip():
                bases.append(Path(e["CURSOR_CONFIG_DIR"]).expanduser())
            elif (e.get("XDG_CONFIG_HOME") or "").strip():
                bases.append(Path(e["XDG_CONFIG_HOME"]).expanduser() / "cursor")
        for base in dict.fromkeys([*bases, Path.home() / ".cursor"]):
            chats = base / "chats"
            here = hashlib.md5(os.path.abspath(cwd).encode(), usedforsecurity=False).hexdigest() if cwd else ""
            if here and (chats / here / session_id / "store.db").is_file() \
                    or any(chats.glob(f"*/{session_id}/store.db")):
                return True
        return False

    def plugin_args(self, dirs: list[str]) -> list[str]:
        if not dirs or "--plugin-dir" not in cli_output(self.binary() or "agent", "--help"):
            return []
        out: list[str] = []
        for d in dirs:
            out += ["--plugin-dir", d]
        return out

    def plugin_dirs_why(self, isolated: bool) -> str:
        return "this Cursor build's `--help` lists no --plugin-dir."

    def streams(self, argv: list[str]) -> bool:
        return "stream-json" in argv   # older builds print their one JSON result at the end

    def _scan(self, output_path: Path) -> dict:
        """The result object, the usage it (or any earlier event) reported, the last assistant text
        and how many characters the agent produced so far."""
        seen = {"result": None, "usage": dict.fromkeys(USAGE_KEYS, 0), "reported": False, "last": "",
                "session_id": "", "chars": 0, "events": 0}
        streamed = dict.fromkeys(USAGE_KEYS, 0)
        try:
            with open(output_path, errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line.startswith("{"):
                        continue
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(ev, dict):
                        continue
                    seen["events"] += 1
                    seen["session_id"] = ev.get("session_id") or seen["session_id"]
                    kind = ev.get("type")
                    if kind == "assistant":
                        content = (ev.get("message") or {}).get("content") or []
                        text = "".join(c.get("text") or "" for c in content if isinstance(c, dict))
                        seen["last"] = text or seen["last"]
                        seen["chars"] += len(text)
                    elif kind == "tool_call":
                        seen["chars"] += len(json.dumps(ev.get("tool_call") or {}))
                    usage = ev.get("usage")
                    if kind in ("result", None) and "result" in ev:
                        seen["result"] = ev
                        if isinstance(usage, dict):
                            seen["usage"] = {k: int(usage.get(k) or 0) for k in USAGE_KEYS}
                            seen["reported"] = True
                    elif isinstance(usage, dict):
                        for k in USAGE_KEYS:
                            streamed[k] += int(usage.get(k) or 0)
        except FileNotFoundError:
            pass
        if not seen["reported"] and any(streamed.values()):
            # Per-message usage only counts when no result totals arrived, or it would count twice.
            seen["usage"], seen["reported"] = streamed, True
        return seen

    def _price(self, usage: dict) -> float:
        (pin, pcached, pout), _ = rate(self.model, self.prices)
        # Cache writes are input billed apart; at 1.25x input (the highest write premium) never under.
        return (usage["inputTokens"] * pin + usage["cacheReadTokens"] * pcached + usage["outputTokens"] * pout
                + usage["cacheWriteTokens"] * pin * 1.25) / 1e6

    def activity(self, output_path, offset=0):
        """stream-json: a `tool_call` event with subtype started is a call, completed its result (an
        error or a non-zero exit code is an error). The call is keyed by its kind (shellToolCall,
        editToolCall, ...), holding its args."""
        lines, offset = jsonl_from(Path(output_path), offset, (b'"tool_call"',))
        events = []
        for ev in lines:
            body = ev.get("tool_call")
            if ev.get("type") != "tool_call" or not isinstance(body, dict) or not body:
                continue
            name, call = next(iter(body.items()))
            call = call if isinstance(call, dict) else {}
            args = call.get("args") if isinstance(call.get("args"), dict) else {}
            payloads = _payloads(name, args)
            tool = name.removesuffix("ToolCall") or name
            head = args.get("command") if tool == "shell" else args.get("path") or args
            ts = ev.get("timestamp_ms")
            out = {"kind": "call", "id": str(ev.get("call_id") or ""), "tool": tool, "input_head": head80(head or ""),
                   "is_error": None, "ts": ts / 1000 if isinstance(ts, (int, float)) else None,
                   "hash": input_hash(tool, head or ""), "command": str(head or "")[:CMD_MAX] if tool == "shell" else ""}
            if ev.get("subtype") == "started":
                events.append({**out, "payloads": payloads})
            elif ev.get("subtype") == "completed":
                res = call.get("result") if isinstance(call.get("result"), dict) else {}
                code = (res.get("success") or {}).get("exitCode") if isinstance(res.get("success"), dict) else None
                failed = any(k in res for k in ("error", "failure", "rejected")) or (isinstance(code, int) and code != 0)
                events.append({**out, "kind": "result", "is_error": failed})
        return events, offset

    def cost_so_far(self, output_path, since=None, env=None):
        seen = self._scan(output_path)
        if seen["reported"]:
            return self._price(seen["usage"])
        if not seen["events"]:
            return None
        # No usage yet: a floor of what the agent wrote (~4 characters a token) at the output rate.
        # It under-counts the context each call reads, but a run writing without end still trips it.
        return self._price({**dict.fromkeys(USAGE_KEYS, 0), "outputTokens": seen["chars"] // 4})

    def parse(self, output_path, stderr_path=None) -> RunUsage:
        u = RunUsage(estimated=True)
        seen = self._scan(Path(output_path))
        data = seen["result"]
        u.session_id = seen["session_id"]
        u.input_tokens = seen["usage"]["inputTokens"]
        u.output_tokens = seen["usage"]["outputTokens"]
        u.cache_read_tokens = seen["usage"]["cacheReadTokens"]
        u.cache_write_tokens = seen["usage"]["cacheWriteTokens"]
        # A run killed before its result keeps the last thing it said.
        u.final_text = (data.get("result") or "") if data else seen["last"]
        if data and (data.get("is_error") or data.get("subtype") not in (None, "success")):
            u.error = str(data.get("result") or data.get("subtype"))[:400]
        err = stderr_tail(stderr_path)
        if not data and err.strip():
            u.error = err.strip()[-400:]   # a failed run prints no result, only its reason on stderr
        # Only reported usage is priced: with none, the daemon books the run's elapsed budget share.
        u.cost_usd = self._price(seen["usage"])
        blob = u.error + " " + err
        if AUTH_RE.search(blob) and not u.output_tokens:
            u.auth_failed = True
        elif LIMIT_RE.search(blob):
            u.limited, u.limit_note = True, LIMIT_RE.search(blob).group(0)
        # Only a failed result on a run that wrote nothing: never stderr alone.
        if data and request_rejected(u.error) and not (u.cost_usd or u.output_tokens or u.auth_failed or u.limited):
            u.request_error = True
        if u.final_text.strip().startswith("{"):
            try:
                u.structured = json.loads(u.final_text)
            except ValueError:
                pass
        if not data and isinstance(u.structured, dict) and isinstance(u.structured.get("actions"), list):
            # Cut off before its result (the runner's budget check) after a whole JSON answer: the
            # daemon may apply it (Daemon._finish_coordinator). A cut-off message never parses.
            u.answer = u.structured
        return u

    def account(self) -> str:
        """'cursor:<hash>' of the login `agent status` names (never the email, a name or a token), or
        'cursor' when the status reads logged in but names no login. While the status cannot be read,
        is not offered or says logged out, the last label seen in this process stands, and before
        any is seen the account is unknown (""), so a logout or a restart is not an account switch."""
        b = self.binary()
        if not b:
            return ""
        key, now = f"{b}|{self.credentials_stamp()}", time.monotonic()
        hit = _ACCOUNT.get(key)
        if hit and now - hit[0] < ACCOUNT_TTL_S:
            return hit[1]
        label = _LAST_LABEL.get(b, "")
        helptext = cli_output(b, "--help")
        if not helptext:
            # --help could not run: ask it again with the status after the TTL, not never.
            _CLI_OUTPUT.pop((b, "--help"), None)
        got = status_check([b, "status"], timeout_s=15) if re.search(r"^\s*status\b", helptext, re.M) else None
        if got is not None and got[0] == 0 and not LOGGED_OUT_RE.search(got[1]):
            label = account_label(status_login(got[1]))
            _LAST_LABEL[b] = label
        _ACCOUNT.clear()
        _ACCOUNT[key] = (now, label)
        return label


EDIT_CALLS = {"editToolCall": "Edit", "writeToolCall": "Write", "deleteToolCall": "Edit"}


def _payloads(name: str, args: dict) -> list[dict]:
    """The hook-shaped calls of one started stream-json tool call (Provider.activity "payloads")."""
    if name == "shellToolCall":
        return [{"tool_name": "Bash", "tool_input": {"command": str(args.get("command") or "")}}]
    if name in EDIT_CALLS:
        return [{"tool_name": EDIT_CALLS[name], "tool_input": {"file_path": str(args.get("path") or "")}}]
    if name == "mcpToolCall":
        server = args.get("providerIdentifier") or args.get("server") or ""
        tool = args.get("toolName") or args.get("name") or ""
        return [{"tool_name": f"mcp__{server}__{tool}", "tool_input": args.get("args") or args.get("arguments")}]
    return []

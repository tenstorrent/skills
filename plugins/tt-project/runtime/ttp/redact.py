# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Masks every stored secret (`ttp secret`) in text the harness keeps or sends on.

The values come from the per-user secrets store (project.load_secrets): in a built-in slot (BUILTIN:
jev, slack, endpoint/<alias>) each field whose name says it holds a credential (key, token, secret,
password), in a custom one (`ttp secret set` or `generate`) every string; each at least MIN_LEN
characters long. Each
becomes `[secret:<slot>]`, e.g. `[secret:endpoint/tt-cloud]`. One helper for every route:

- the database (Db.x): event, note, notify, message and hand-off text, so every `ttp` command and the
  chat relay that read it back never show a value;
- a run's folder and its agent's own session transcript once the run ends (scrub_run: the runner,
  and the daemon again before it reads the hand-off);
- text sent to a helper model or service (Jev, Slack) and endpoint client errors.

Every private-key block (PEM `-----BEGIN <type> PRIVATE KEY-----` to its END line, PGP PRIVATE KEY
BLOCK) becomes KEY_MASK the same way, stored or not, raw or JSON-escaped; one cut off before its END
line (a lone BEGIN line too) is masked to the end of its string (the first character a key body never
holds, such as the quote closing a JSON string), so JSONL stays valid; a lone END line is masked with
the base64 lines right above it (a block whose start output trimming cut off).

Nothing here prints or logs a value: callers get the masked text, or a count.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

MIN_LEN = 8                      # shorter values would mask ordinary words
SECRET_FIELD_RE = re.compile(r"key|token|secret|passw", re.I)
MAX_FILE_BYTES = 256 << 20       # a run file bigger than this is left alone (never seen in practice)
BUILTIN = ("jev", "slack")       # slots whose other fields (via, user_id, url) are not secret
_cache: dict[str, Any] = {"sig": None, "pairs": []}
KEY_MASK = "[private key redacted]"
_KEY_TYPE = r"(?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?"
# A key body: base64, PEM and PGP armor headers (Proc-Type, DEK-Info, Version, Comment) and line ends,
# raw or JSON-escaped (once or more: JSON in a JSON string); never a quote or a lone backslash.
_KEY_BODY = r"(?:[A-Za-z0-9+/=:,.()\- \t\r\n]|\\+[nrt/]|\\+u[0-9a-fA-F]{4})"
_KEY_RE = (rf"-{{4,5}} ?BEGIN {_KEY_TYPE} ?-{{4,5}}{_KEY_BODY}*?"
           rf"(?:-{{4,5}} ?END {_KEY_TYPE} ?-{{4,5}}|(?!{_KEY_BODY}))")
KEY_RE = re.compile(_KEY_RE)
_KEY_END_RE = re.compile(rf"-{{4,5}} ?END {_KEY_TYPE} ?-{{4,5}}")
# One base64 line, raw or JSON-escaped, from a line start (or the text's) to its line end last.
_KEY_LINE_RE = re.compile(r"(?:^|(?<=\n)|(?<=\\n))[A-Za-z0-9+/=]+(?:\r?\n|(?:\\+r)?\\+n)\Z")


def custom(slot: str) -> bool:
    """A slot `ttp secret set` or `generate` made: every string in it is secret."""
    from .endpoint import PREFIX
    return slot not in BUILTIN and not slot.startswith(PREFIX)


def _sig(path: Path):
    try:
        st = path.stat()
    except OSError:
        return None
    return (str(path), st.st_mtime_ns, st.st_size, st.st_ino)


def _collect(slot: str, v: Any, field: str, out: list[tuple[str, str]], every: bool = False) -> None:
    if isinstance(v, dict):
        for k, x in v.items():
            _collect(slot, x, str(k), out, every)
    elif isinstance(v, list):
        for x in v:
            _collect(slot, x, field, out, every)
    elif isinstance(v, str) and len(v.strip()) >= MIN_LEN and (every or SECRET_FIELD_RE.search(field)):
        out.append((v.strip(), slot))


def pairs() -> list[tuple[str, str]]:
    """(value, slot) for every stored secret, longest first so a value inside another is masked whole.
    Re-read only when the store changes. Never raises: every DB write and daemon log line calls this,
    so a store it cannot read or parse (wrong owner or mode, a folder, bad bytes, not a JSON object)
    keeps the last good pairs (none if never read) and is read again on the next call."""
    from .project import load_secrets, secrets_path
    try:
        sig = _sig(secrets_path())
        if sig == _cache["sig"]:
            return _cache["pairs"]
        data = load_secrets() if sig else {}
        if not isinstance(data, dict):
            return _cache["pairs"]
        out: list[tuple[str, str]] = []
        for slot, v in data.items():
            _collect(str(slot), v, "key" if isinstance(v, str) else "", out, custom(str(slot)))
    except Exception:
        return _cache["pairs"]
    _cache["pairs"] = sorted(set(out), key=lambda p: -len(p[0]))
    _cache["sig"] = sig
    return _cache["pairs"]


def mask(slot: str) -> str:
    return f"[secret:{slot}]"


def _mask_keys(text: str) -> tuple[str, int]:
    if "PRIVATE KEY" not in text:
        return text, 0
    text, n = KEY_RE.subn(KEY_MASK, text)
    out, at = [], 0
    for m in _KEY_END_RE.finditer(text):   # END lines left alone: no BEGIN before them
        start, lines = m.start(), []
        while start > at and (b := _KEY_LINE_RE.search(text[max(at, start - 400):start])):
            line = b.group()
            head = text[:start - len(line)]
            if (lines and len(line.rstrip("\\nr\r\n")) < 16) or (
                    start - len(line) > at and not head.endswith(("\n", "\\n"))):
                break   # a short line above the last, or the end of a longer line (prose)
            lines.append(line)
            start -= len(line)
        if not any(len(x.rstrip("\\nr\r\n")) >= 40 for x in lines):
            start = m.start()   # no full base64 line above it: only the END line
        out += [text[at:start], KEY_MASK]
        at, n = m.end(), n + 1
    return "".join(out) + text[at:], n


def scrub_keys(text: str) -> str:
    """`text` with every private-key block, and any lone BEGIN or END line of one, masked (KEY_MASK)."""
    return _mask_keys(text)[0]


def scrub(text: Any) -> Any:
    """`text` with every private-key block and stored secret value masked; anything but a str comes back
    as it was."""
    if not isinstance(text, str) or not text:
        return text
    text = scrub_keys(text)
    for value, slot in pairs():
        if value in text:
            text = text.replace(value, mask(slot))
    return text


def scrub_obj(obj: Any) -> Any:
    """A JSON-like value with every string in it scrubbed (keys too)."""
    if isinstance(obj, str):
        return scrub(obj)
    if isinstance(obj, dict):
        return {scrub(k): scrub_obj(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return type(obj)(scrub_obj(v) for v in obj)
    return obj


def _variants(value: str) -> list[str]:
    """The value as it appears in a file: as is, and JSON-escaped when it holds quotes or backslashes."""
    esc = json.dumps(value)[1:-1]
    return [value] if esc == value else [value, esc]


def scrub_file(path: Path) -> int:
    """Masks every private-key block and stored secret value in the file, rewritten whole (same mode)
    only when one is in it. Returns how many it masked; 0 for a missing, unreadable or oversized file."""
    found = pairs()
    try:
        if not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_FILE_BYTES:
            return 0
        data = path.read_bytes()
    except OSError:
        return 0
    n = 0
    if b"PRIVATE KEY" in data:   # bytes that are not UTF-8 come back as they were (surrogateescape)
        text, n = _mask_keys(data.decode("utf-8", "surrogateescape"))
        data = text.encode("utf-8", "surrogateescape")
    for value, slot in found:
        for v in _variants(value):
            b = v.encode()
            c = data.count(b)
            if c:
                data, n = data.replace(b, mask(slot).encode()), n + c
    if n:
        from .project import durable_write
        try:
            durable_write(path, data)
        except OSError:
            return 0
    return n


def session_id(output: Path) -> str:
    """The agent's session id from the start of a run's output (Claude `session_id`, Codex `thread_id`)."""
    try:
        with open(output, "rb") as f:
            for _ in range(50):
                line = f.readline()
                if not line:
                    break
                m = re.search(rb'"(?:session_id|thread_id)"\s*:\s*"([A-Za-z0-9-]{8,80})"', line)
                if m:
                    return m.group(1).decode()
    except OSError:
        pass
    return ""


def scrub_run(run_dir: Path) -> int:
    """Masks private keys and stored secrets in every file of a finished run's folder (output, logs,
    hand-off) and in the agent's own session transcript for that run. Returns how many it masked; safe
    to run twice."""
    run_dir = Path(run_dir)
    n = 0
    try:
        files = [f for f in run_dir.rglob("*") if f.is_file() and not f.is_symlink()]
    except OSError:
        files = []
    for f in files:
        n += scrub_file(f)
    try:
        spec = json.loads((run_dir / "run.json").read_text())
    except (OSError, ValueError):
        spec = {}
    sid = session_id(run_dir / "output.jsonl")
    if sid and isinstance(spec, dict) and spec.get("provider"):
        try:
            from .providers import get_provider
            paths = get_provider(spec["provider"]).session_paths(sid, str(spec.get("cwd") or ""),
                                                                 spec.get("env") or {})
        except Exception:
            paths = []
        for p in paths:
            n += scrub_file(p)
    return n


def private_dir(path: Path) -> Path:
    """Makes `path` (and any missing parents) a folder only its owner can open (0700)."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    return path

# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Masks every stored secret (`ttp secret`) in text the harness keeps or sends on.

The values come from the per-user secrets store (project.load_secrets): each field whose name says
it holds a credential (key, token, secret, password) and is at least MIN_LEN characters long. Each
becomes `[secret:<slot>]`, e.g. `[secret:endpoint/tt-cloud]`. One helper for every route:

- the database (Db.x): event, note, notify, message and hand-off text, so every `ttp` command and the
  chat relay that read it back never show a value;
- a run's folder and its agent's own session transcript once the run ends (scrub_run: the runner,
  and the daemon again before it reads the hand-off);
- text sent to a helper model or service (Jev, Slack) and endpoint client errors.

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
_cache: dict[str, Any] = {"sig": None, "pairs": []}


def _sig(path: Path):
    try:
        st = path.stat()
    except OSError:
        return None
    return (str(path), st.st_mtime_ns, st.st_size, st.st_ino)


def _collect(slot: str, v: Any, field: str, out: list[tuple[str, str]]) -> None:
    if isinstance(v, dict):
        for k, x in v.items():
            _collect(slot, x, str(k), out)
    elif isinstance(v, list):
        for x in v:
            _collect(slot, x, field, out)
    elif isinstance(v, str) and len(v.strip()) >= MIN_LEN and SECRET_FIELD_RE.search(field):
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
            _collect(str(slot), v, "key" if isinstance(v, str) else "", out)
    except Exception:
        return _cache["pairs"]
    _cache["pairs"] = sorted(set(out), key=lambda p: -len(p[0]))
    _cache["sig"] = sig
    return _cache["pairs"]


def mask(slot: str) -> str:
    return f"[secret:{slot}]"


def scrub(text: Any) -> Any:
    """`text` with every stored secret value masked; anything but a str comes back as it was."""
    if not isinstance(text, str) or not text:
        return text
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
    """Masks every stored secret value in the file, rewritten whole (same mode) only when one is in it.
    Returns how many values it masked; 0 for a missing, unreadable or oversized file."""
    found = pairs()
    if not found:
        return 0
    try:
        if not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_FILE_BYTES:
            return 0
        data = path.read_bytes()
    except OSError:
        return 0
    n = 0
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
    """Masks stored secrets in every file of a finished run's folder (output, logs, hand-off) and in the
    agent's own session transcript for that run. Returns how many values it masked; safe to run twice."""
    if not pairs():
        return 0
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

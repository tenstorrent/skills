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
  and the daemon again before it reads the hand-off, within STEP_FILE_BYTES per file and STEP_SCRUB_S
  per run: what is left over is masked by a detached job, scrub_later);
- text sent to a helper model or service (Jev, Slack) and endpoint client errors.

Every private-key block (PEM, from its `BEGIN <type> PRIVATE KEY` line to its END line, PGP PRIVATE KEY
BLOCK) becomes KEY_MASK the same way, stored or not, raw or JSON-escaped. One cut off before its END
line (a lone BEGIN line too) is masked: in raw text through the last key line after it (base64, or a
header such as `Proc-Type: 4,ENCRYPTED`), so prose after a quoted marker line stays; inside a
JSON-escaped string to the end of that string (the first character a key body never holds, such as the
quote closing it), so JSONL stays valid. A lone END line is masked with the base64 lines right above it
(a block whose start output trimming cut off).

Nothing here prints or logs a value: callers get the masked text, or a count.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

MIN_LEN = 8                      # shorter values would mask ordinary words
SECRET_FIELD_RE = re.compile(r"key|token|secret|passw", re.I)
MAX_FILE_BYTES = 256 << 20       # a run file bigger than this is left alone, and named (never seen in practice)
# Inside a daemon step (a run's end, the detached-job sweep): a file bigger than this, and every file not
# reached within STEP_SCRUB_S of a run's scrub, is masked by a detached job (scrub_later), never read there.
STEP_FILE_BYTES = 8 << 20
STEP_SCRUB_S = 10.0
SESSION_HEAD_BYTES = 4 << 20     # session_id reads no more of a run's output than this
BUILTIN = ("jev", "slack")       # slots whose other fields (via, user_id, url) are not secret
_cache: dict[str, Any] = {"sig": None, "pairs": []}
KEY_MASK = "[private key redacted]"
_KEY_TYPE = r"(?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?"
# A key body: base64, PEM and PGP armor headers (Proc-Type, DEK-Info, Version, Comment) and line ends,
# raw or JSON-escaped (once or more: JSON in a JSON string); never a quote or a lone backslash.
_KEY_BODY = r"(?:[A-Za-z0-9+/=:,.()\- \t\r\n]|\\+[nrt/]|\\+u[0-9a-fA-F]{4})"
# Cut off inside a JSON-escaped string (an escaped line end after BEGIN): to the end of that string.
_ESC_BODY = r"(?:[A-Za-z0-9+/=:,.()\- \t]|\\+[nrt/]|\\+u[0-9a-fA-F]{4})"
_ESC_CUT = rf"{_ESC_BODY}*?\\+n{_ESC_BODY}*(?!{_ESC_BODY})"
# Cut off in raw text: base64 on the BEGIN line, then each line that looks like a key line: base64 (a
# partial one when cut mid-line: 16 or more of it) or a header (Name: value), after a line number as
# `cat -n` or a file viewer prints it, if any; blank lines only before another key line. Prose stops it.
# Each line matches one way only (spaces never split between two parts), so a failing line costs linear time.
_B64 = r"[A-Za-z0-9+/=]"
_LINE_START = r"\r?\n[ \t]*(?:[0-9]+(?:\t|\u2192)[ \t]*)?"
_RAW_BLANK = rf"{_LINE_START}(?=\r?\n)"
_RAW_LINE = rf"{_LINE_START}(?:{_B64}+[ \t]*(?=\r?\n|\Z)|{_B64}{{16,}}|[A-Za-z][A-Za-z0-9-]*:[ \t][^\r\n\"'`\\]*)"
_RAW_CUT = rf"(?:[ \t]+{_B64}{{16,}})*(?:[ \t]+(?=\r?\n))?(?:(?:{_RAW_BLANK})*{_RAW_LINE})*"
_KEY_RE = (rf"-{{4,5}} ?BEGIN {_KEY_TYPE} ?-{{4,5}}"
           rf"(?:{_KEY_BODY}*?-{{4,5}} ?END {_KEY_TYPE} ?-{{4,5}}|{_ESC_CUT}|{_RAW_CUT})")
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
        while start > at and len(lines) < 400 and (b := _KEY_LINE_RE.search(text, max(at, start - 400), start)):
            line, s0 = b.group(), b.start()
            if (lines and len(line.rstrip("\\nr\r\n")) < 16) or (
                    s0 > at and text[s0 - 1] != "\n" and text[s0 - 2:s0] != "\\n"):
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


def scrub_file(path: Path, cap: int = MAX_FILE_BYTES, left: list | None = None) -> int:
    """Masks every private-key block and stored secret value in the file, rewritten whole (same mode)
    only when one is in it. Returns how many it masked; 0 for a missing, unreadable or oversized file.
    It reads at most `cap` bytes: a bigger file is added to `left` (scrub_later, or named by the caller)."""
    found = pairs()
    path = Path(path)
    try:
        if not path.is_file() or path.is_symlink():
            return 0
        data = None
        if path.stat().st_size <= cap:
            with open(path, "rb") as f:
                data = f.read(cap + 1)
        if data is None or len(data) > cap:   # over the cap, or grown past it since
            if left is not None:
                left.append(path)
            return 0
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
            head = f.read(SESSION_HEAD_BYTES)
    except OSError:
        return ""
    for line in head.split(b"\n", 50)[:50]:
        m = re.search(rb'"(?:session_id|thread_id)"\s*:\s*"([A-Za-z0-9-]{8,80})"', line)
        if m:
            return m.group(1).decode()
    return ""


# A started or queued job in a run's folder (`ttp detach`: <name>.started or .queued; `ttp checks --detach`:
# checks.pid) that has neither <name>.rc nor <name>.lost yet still writes <name>.log and <name>.out.
_JOB_MARKS = (".started", ".queued")


def live_job_logs(files: list[Path]) -> set[Path]:
    """The logs among a folder's `files` a detached job still writes. A rewrite renames a new file over
    one, so the job's later lines would go to the old, unlinked file: they are left for detach.close."""
    out: set[Path] = set()
    for f in files:
        if f.suffix in _JOB_MARKS or f.name == "checks.pid":
            stem = f.name[:-len(f.suffix)]
            if not f.with_name(stem + ".rc").exists() and not f.with_name(stem + ".lost").exists():
                out |= {f.with_name(stem + ".log"), f.with_name(stem + ".out")}
    return out


def scrub_run(run_dir: Path, step: bool = False, left: list | None = None) -> int:
    """Masks private keys and stored secrets in every file of a finished run's folder (output, logs,
    hand-off) but the logs a detached job still writes (live_job_logs), and in the agent's own session
    transcript for that run. Returns how many it masked; safe to run twice.

    The files it did not mask go to `left`: each over MAX_FILE_BYTES, and in a daemon `step` each over
    STEP_FILE_BYTES, or, once past STEP_SCRUB_S, the folder itself in place of all of them; for scrub_later."""
    run_dir = Path(run_dir)
    cap, until = (STEP_FILE_BYTES, time.monotonic() + STEP_SCRUB_S) if step else (MAX_FILE_BYTES, None)
    left = [] if left is None else left
    n, files, start = 0, [], len(left)

    def out_of_time() -> bool:   # then the detached job takes the whole folder (scrub_run again: safe twice)
        if until is None or time.monotonic() <= until:
            return False
        del left[start:]
        left.append(run_dir)
        return True
    try:
        for f in run_dir.rglob("*"):
            if out_of_time():
                return n
            if f.is_file() and not f.is_symlink():
                files.append(f)
        live = live_job_logs(files)
    except OSError:
        files, live = [], set()
    for f in files:
        if out_of_time():
            return n
        if f not in live:
            n += scrub_file(f, cap, left)
    try:
        with open(run_dir / "run.json", "rb") as f:
            spec = json.loads(f.read(cap))
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
            if out_of_time():
                return n
            n += scrub_file(p, cap, left)
    return n


def scrub_later(paths: list, log: Path) -> int | None:
    """Masks `paths` (files, or a run's folder: scrub_run) in a detached `python -m ttp.redact` job with
    no size cap or time limit, outside the daemon's tick; its counts, and any file it still had to leave
    (over MAX_FILE_BYTES), go to `log`. The job's pid, or None when it could not start."""
    if not paths:
        return None
    runtime = str(Path(__file__).resolve().parent.parent)
    try:
        with open(log, "ab") as out:   # own session: the masking job outlives the daemon tick that starts it
            return subprocess.Popen([sys.executable, "-m", "ttp.redact", *map(str, paths)], cwd=runtime,
                                    env={**os.environ, "PYTHONPATH": runtime}, stdin=subprocess.DEVNULL,
                                    stdout=out, stderr=out, start_new_session=True).pid
    except OSError:
        return None


def main(argv: list[str]) -> int:
    left: list = []
    n = sum(scrub_run(Path(a), left=left) if Path(a).is_dir() else scrub_file(Path(a), left=left) for a in argv)
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} redact: masked {n} in {len(argv)} path(s)", flush=True)
    for f in left:
        print(f"redact: not masked, over {MAX_FILE_BYTES >> 20} MiB: {f}", flush=True)
    return 1 if left else 0


def private_dir(path: Path) -> Path:
    """Makes `path` (and any missing parents) a folder only its owner can open (0700)."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    return path


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

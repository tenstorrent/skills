# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""Messages for a project on another machine that could not be sent yet.

`ttp say` to a project this machine cannot reach right now (ssh exits 255) lands here, one JSON
line per message, in order: ~/.tt-project/outbox/<project>.jsonl, mode 0600. Each carries a client
id; the project stores it with the message and skips an id it has seen, so a resend after a
dropped confirmation is never a duplicate. An entry leaves the file only once the project has
confirmed it. One the project rejects outright moves to <project>.rejected.jsonl, kept, so it
neither blocks the messages behind it nor disappears.
"""
from __future__ import annotations

import fcntl
import json
import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from . import project


def folder() -> Path:
    return project.HOME_DIR / "outbox"


def path(name: str) -> Path:
    return folder() / f"{name}.jsonl"


def new_id() -> str:
    return str(uuid.uuid4())


@contextmanager
def locked(name: str) -> Iterator[None]:
    """One process at a time appends to or flushes a project's outbox."""
    folder().mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(folder() / f"{name}.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def entries(name: str) -> list[dict]:
    try:
        lines = path(name).read_text().splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue    # a line cut short by a crash mid-append: the rest still go out
    return out


def count(name: str) -> int:
    return len(entries(name))


def _append(p: Path, entry: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, (json.dumps(entry, sort_keys=True) + "\n").encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    project.fsync_dir(p.parent)


def add(name: str, argv: list[str], client_id: str, chat: str | None) -> dict:
    entry = {"id": client_id, "argv": argv, "ts": time.time(), "chat": chat}
    with locked(name):
        _append(path(name), entry)
    return entry


def _rewrite(name: str, keep: list[dict]) -> None:
    p = path(name)
    if not keep:
        _unlink(p)
        return
    project.durable_write(p, "".join(json.dumps(e, sort_keys=True) + "\n" for e in keep), mode=0o600)


def _unlink(p: Path) -> None:
    try:
        p.unlink()
    except FileNotFoundError:
        pass


def drop(name: str, client_id: str, rejected: str = "") -> None:
    """Remove a delivered entry (caller holds the lock). With `rejected`, keep it aside instead."""
    rest = entries(name)
    if rejected:
        for e in rest:
            if e.get("id") == client_id:
                _append(folder() / f"{name}.rejected.jsonl", {**e, "rejected": rejected})
    _rewrite(name, [e for e in rest if e.get("id") != client_id])

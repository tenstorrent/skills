"""A burst of relayer messages starts one coordinator turn, and an order or a question in it still starts one fast.

A message relayed by another agent (`ttp say`: provenance cli-peer or cli-legacy) that carries neither an order
nor a question (status, an acknowledgement, a relayer's findings) may wait up to coordinator.relay_window_s
(default 90; 0: off) after the oldest unhandled message, so the messages of one burst share one turn. Every
other message wakes at the usual debounce (coordinator.debounce_s after the newest, at most four times that
after the oldest), which is shorter than the window: a message from the user's own channel (Slack, the web
page, the system), and a relayed one with a question mark or an order cue (urgent()). A message that waits
stays unhandled on disk, so a restart keeps it; the window runs from its timestamp, never past the window.
Over-reading a cue costs only the turn the message would have started anyway, so the cues are generous."""
from __future__ import annotations

import re

DEFAULT_S = 90.0
RELAYED = ("cli-peer", "cli-legacy")   # prguard.PROVENANCES a relaying agent writes with `ttp say`
# An imperative or a demand anywhere a sentence or line starts, or a word that asks for action now.
_VERBS = ("add|answer|ask|build|cancel|change|check|close|continue|delete|deploy|do|don't|drop|finish|fix|"
          "follow|give|go|hold|keep|land|let|look|make|measure|merge|move|never|notify|open|pause|please|post|"
          "push|put|release|remove|reply|report|restart|resume|retry|revert|review|run|send|set|ship|show|"
          "start|stop|switch|take|tell|test|try|undo|update|use|wait|write")
_ORDER = re.compile(rf"(?:^|[.!;:\n]\s*|\b(?:and|then)\s+)(?:{_VERBS})\b"
                    r"|\b(?:must|should|need to|needs to|have to|asap|urgent|immediately|right now)\b",
                    re.IGNORECASE)


def window_s(cfg: dict) -> float:
    try:
        return max(float((cfg.get("coordinator") or {}).get("relay_window_s", DEFAULT_S)), 0.0)
    except (TypeError, ValueError):
        return DEFAULT_S


def urgent(m) -> bool:
    """Whether message `m` (a messages row) wakes at the debounce: not relayed, or relayed with an order or a
    question in the user's words or the relayer's context."""
    if str(m["provenance"] or "") not in RELAYED:
        return True
    said = f"{m['text'] or ''}\n{m['context'] or ''}"
    return "?" in said or bool(_ORDER.search(said))


def until(cfg: dict, msgs: list, now: float) -> float | None:
    """When the relay window over these unhandled messages ends, or None when the turn may start now."""
    win = window_s(cfg)
    if win <= 0 or not msgs or any(urgent(m) for m in msgs):
        return None
    end = min(float(m["ts"]) for m in msgs) + win
    return end if now < end else None

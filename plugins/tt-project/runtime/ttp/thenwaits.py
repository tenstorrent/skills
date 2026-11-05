"""A waiting hand-off's `then`: the next waits of a known sequence (checks, then landed; a job, then its
verdict), so the daemon moves from one stage to the next with no model run.

`then` is a list of stages `{"retry_when": ..., "on_pass_cmd"?: ..., "retry_after_s"?: ...}` that follow
the hand-off's own retry_when (and on_pass_cmd). When a stage's probe passes, and its on_pass_cmd, if it
names one, exits 0 without writing a hand-off of its own, the daemon makes the next stage the task's wait
(Daemon._next_stage). The task wakes only as any wait does: a stage's on_pass_cmd that fails, a probe
that is broken, failed checks, or the last stage passing without a done hand-off (the hand-off's
`on_pass`, if it names one, is recorded then). The remaining stages and how many have passed
(STAGE_KEY) live in the task's stored hand-off, so a daemon restart or a host reboot keeps the stage.
"""
from __future__ import annotations

KEY = "then"
STAGE_KEY = "then_stage"     # how many stages the daemon has moved past, in the task's stored hand-off
REFUSED_KEY = "then_refused"
MAX_STAGES = 4
STAGE_FIELDS = ("retry_when", "on_pass_cmd", "retry_after_s")


def problem(result: dict) -> str | None:
    """Why a hand-off's `then` is refused, None when it names none or a valid one."""
    if not isinstance(result, dict) or KEY not in result:
        return None
    then = result[KEY]
    if not isinstance(then, list) or not then:
        return f"`{KEY}` must be a non-empty list of stages ({{\"retry_when\": ...}})"
    if len(then) > MAX_STAGES:
        return f"`{KEY}` names {len(then)} stages; at most {MAX_STAGES}"
    if result.get("status") != "waiting":
        return f"`{KEY}` is only for a waiting hand-off"
    rw = result.get("retry_when")
    if not isinstance(rw, str) or not rw.strip():
        return f"`{KEY}` follows the hand-off's own retry_when, and it names none"
    from .coordinator import check_probe
    for i, stage in enumerate(then, 1):
        if not isinstance(stage, dict):
            return f"`{KEY}` stage {i} must be an object, not {type(stage).__name__}"
        if extra := sorted(set(stage) - set(STAGE_FIELDS)):
            return f"`{KEY}` stage {i} names {', '.join(map(str, extra))}; a stage names only {', '.join(STAGE_FIELDS)}"
        probe = stage.get("retry_when")
        if not isinstance(probe, str) or not probe.strip():
            return f"`{KEY}` stage {i} needs a retry_when (one shell command)"
        try:
            check_probe(probe, "retry_when")
            if "on_pass_cmd" in stage:
                cmd = stage["on_pass_cmd"]
                if not isinstance(cmd, str) or not cmd.strip():
                    return f"`{KEY}` stage {i}'s on_pass_cmd must be one shell command"
                check_probe(cmd, "on_pass_cmd")
        except ValueError as e:
            return f"`{KEY}` stage {i}: {e}"
        ras = stage.get("retry_after_s")
        if ras is not None and (isinstance(ras, bool) or not isinstance(ras, (int, float)) or ras <= 0):
            return f"`{KEY}` stage {i}'s retry_after_s must be a positive number of seconds"
    return None


def advance(prev: dict) -> dict | None:
    """The stored hand-off `prev` with its next stage as its wait, or None when it has no next stage.
    The run's own retry_when and on_pass_cmd stay as `<key>_orig` (daemon._same_probe), so the run's
    result.json is still the hand-off the task waits on."""
    then = prev.get(KEY) if isinstance(prev, dict) else None
    if not isinstance(then, list) or not then or not isinstance(then[0], dict):
        return None
    stage, rest = then[0], then[1:]
    nxt = {k: v for k, v in prev.items() if k not in (KEY, "on_pass_cmd", "woke")}
    nxt.setdefault("retry_when_orig", prev.get("retry_when"))
    nxt.setdefault("on_pass_cmd_orig", prev.get("on_pass_cmd"))
    nxt["retry_when"] = stage["retry_when"]
    if stage.get("on_pass_cmd"):
        nxt["on_pass_cmd"] = stage["on_pass_cmd"]
    if stage.get("retry_after_s") is not None:
        nxt["retry_after_s"] = stage["retry_after_s"]
    if rest:
        nxt[KEY] = rest
    nxt[STAGE_KEY] = int(prev.get(STAGE_KEY) or 0) + 1
    return nxt


def at_end(prev: dict) -> bool:
    """Whether the stored hand-off waits on the last stage of a `then` list the daemon moved through."""
    return isinstance(prev, dict) and bool(prev.get(STAGE_KEY)) and not prev.get(KEY)

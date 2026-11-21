# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""A live mini-swe-agent worker for the code arm's GLM side (codearm.py, `route.code_sonnet.glm.harness`).

Claude Code runs the arm's model through the chat bridge today, and landed 0 of 8 in the benchmark; the arm's
live harness is mini-swe-agent, whose command layer is the guard (glmenv.GuardedEnvironment: glmguard's
side-effect rules before every command). apply() turns a run's spec into a mini run:

- the agent is the harness install (benchrun.harness_ready, a folder the plugin owns, never ~/.local), launched
  by a script that reads the run's prompt from stdin (prompt.md, the system text leading it);
- its model is the chat bridge's OpenAI-compatible relay (`openai_env`), so the run's cost and tokens are the
  bridge's, cached prompt tokens priced at the cache-read rate (chatbridge, billing) like any chat-provider run;
- its environment class is ttp.glmenv.GuardedEnvironment, with a per-command timeout fit for a build or a test;
- a harness that cannot be installed ends the run at once as a hand-back to Opus (glmguard.hit), never as a
  failed attempt.

The hand-off is the worker prompt's own (result.json in the run folder); the runner, the daemon's bounce path
and `ttp push` treat the run like any other on the arm."""
from __future__ import annotations

import json
import shlex
from pathlib import Path

HARNESS = "mini-swe-agent"
COMMAND_TIMEOUT_S = 900   # one command (a build, a test), over mini's 30 s default
STEP_LIMIT = 0            # no step cap: the run's own timeout, stall and budget end it


def _installed(root: Path) -> str:
    """The newest mini-swe-agent version already installed under `root`, '' when none is."""
    best = ""
    try:
        for f in root.glob(f"{HARNESS}-*/harness.json"):
            ver = f.parent.name[len(HARNESS) + 1:]
            if ver and (not best or [int(x) if x.isdigit() else 0 for x in ver.split(".")]
                        > [int(x) if x.isdigit() else 0 for x in best.split(".")]):
                best = ver
    except OSError:
        return ""
    return best


def install(version: str = "", root: Path | None = None) -> dict:
    """The installed harness {kind, version, dir, bin}: `version`, else the newest already installed, else the
    current release. Raises benchrun.BenchError when it cannot be had."""
    from . import benchrun
    base = Path(root or benchrun.HARNESS_DIR)
    return benchrun.harness_ready(HARNESS, version or _installed(base), base)


def digest_on(cfg: dict) -> bool:
    """`route.code_sonnet.glm.digest`: a command's long output reaches the model as a digest (glmenv.digest).
    Off by default; the dry run's GLM arm turns it on."""
    got = ((cfg.get("route") or {}).get("code_sonnet") or {}).get("glm")
    return isinstance(got, dict) and got.get("digest") is True


def files(run_dir: Path, inst: dict, model: str, runtime_dir: str, digest: bool = False) -> list[str]:
    """Write the launch script and the guarded config into `run_dir`; returns the argv. The prompt is read from
    stdin (the `-t` task), the trajectory goes to trajectory.json."""
    q = shlex.quote
    from . import glmenv
    conf = ("agent:\n  mode: yolo\n  cost_limit: 0\n"
            f"  step_limit: {STEP_LIMIT}\n"
            "model:\n  cost_tracking: ignore_errors\n  model_kwargs:\n    drop_params: true\n"
            f"environment:\n  environment_class: {glmenv.CLASS_PATH}\n  timeout: {COMMAND_TIMEOUT_S}\n")
    from .project import durable_write
    durable_write(run_dir / "mini.yaml", conf)
    body = ("export MSWEA_CONFIGURED=true MSWEA_SILENT_STARTUP=1 MSWEA_COST_TRACKING=ignore_errors "
            + (f"{glmenv.DIGEST_ENV}=1 " if digest else "") +
            f"MSWEA_GLOBAL_CONFIG_DIR={q(str(run_dir / 'mswea'))} PYTHONPATH={q(runtime_dir)}\n"
            f"exec {q(inst['bin'])} -y --exit-immediately -l 0 -m {q('openai/' + model)} -c mini.yaml "
            f"-c {q(str(run_dir / 'mini.yaml'))} -o {q(str(run_dir / 'trajectory.json'))} -t \"$(cat)\"\n")
    durable_write(run_dir / "harness.sh", "#!/bin/sh\n" + body)
    return ["/bin/sh", str(run_dir / "harness.sh")]


def wanted(cfg: dict) -> str:
    """The version setting (`route.code_sonnet.glm.harness_version`), '' for the newest installed or current."""
    got = ((cfg.get("route") or {}).get("code_sonnet") or {}).get("glm")
    return str(got.get("harness_version") or "") if isinstance(got, dict) else ""


def apply(spec: dict, run_dir: Path, cfg: dict, runtime_dir: str, budget_usd=None, install_fn=None) -> str:
    """Make `spec` (a GLM-arm worker's run spec, already written as prompt.md) a mini-swe-agent run; '' on
    success, else why the harness is unavailable (the run is then ended as a hand-back to Opus)."""
    from . import glmguard
    run_dir = Path(run_dir)
    try:
        inst = (install_fn or install)(wanted(cfg))
    except Exception as e:   # BenchError, a missing pip or network, a timeout
        why = f"harness unavailable: {str(e)[:160]}"
        glmguard.hit(str(run_dir), why)
        spec["argv"] = ["/bin/sh", "-c", "exit 0"]
        spec.pop("stdin", None)
        return why
    lead = ""
    try:
        lead = (run_dir / "system.md").read_text()
    except OSError:
        pass
    prompt = (run_dir / "prompt.md").read_text()
    if lead and lead not in prompt:
        from .project import durable_write
        durable_write(run_dir / "prompt.md", lead + "\n\n" + prompt)
    spec["argv"] = files(run_dir, inst, str(spec.get("model") or ""), runtime_dir, digest_on(cfg))
    spec.pop("stdin", None)   # mini reads plain text, not Claude Code's stream-json
    spec["openai_env"] = True
    spec["harness"] = {"kind": HARNESS, "version": inst.get("version")}
    if spec.get("budget_usd") is None and budget_usd is not None:
        spec["budget_usd"] = budget_usd   # mini has no cap of its own: the runner enforces it on the bridge's counts
    return ""


def cost_so_far(bridge, prov) -> float | None:
    """The run's spend so far, from the bridge's token counts (cached prompt tokens at the cache-read rate)."""
    from . import benchrun
    usage = (bridge.stats() or {}).get("usage") or {}
    return float(benchrun.harness_spend(prov, usage)["cost_usd"]) if usage.get("calls") else None


def context_per_call(usage: dict) -> int:
    """The average context a call carried: prompt tokens (fresh and cached) over the bridge's call count. The
    digest's measure (glmenv.digest): the same task with it on and off."""
    calls = int(usage.get("calls") or 0)
    if not calls:
        return 0
    return round((int(usage.get("input_tokens") or 0) + int(usage.get("cache_read_input_tokens") or 0)) / calls)


def step_record(run_dir, seen: int = 0) -> tuple[list[dict], int, dict]:
    """A running mini-swe-agent run's step record: (its new steps past message `seen` as provider
    activity events (providers.base.Provider.activity's shape), the cursor to read from next, its
    usage so far). The trajectory is saved after every step, so a step is an assistant message's
    tool calls (a `call` each, its id, input hash and shell command) and the tool message that
    answers one (a `result`, failed when its returncode is not 0). ([], seen, {}) while the run has
    no readable trajectory."""
    try:
        msgs = json.loads((Path(run_dir) / "trajectory.json").read_text()).get("messages") or []
    except (OSError, ValueError, AttributeError):
        return [], seen, {}
    if not isinstance(msgs, list):
        return [], seen, {}
    if len(msgs) < seen:   # a replaced record (a restarted run): read it afresh
        seen = 0
    from .chatbridge import tool_input, usage_of
    from .providers.base import CMD_MAX, head80, input_hash
    events, usage = [], {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0}
    for i, m in enumerate(msgs):
        if not isinstance(m, dict):
            continue
        extra = m.get("extra") or {}
        if isinstance((resp := extra.get("response") or {}), dict) and isinstance(resp.get("usage"), dict):
            usage["calls"] += 1
            for k, v in usage_of(resp["usage"]).items():
                usage[k] = usage.get(k, 0) + int(v)
        if i < seen or m.get("role") == "exit":
            continue
        if m.get("role") == "tool":
            rc = extra.get("returncode")
            if rc is None:
                try:
                    rc = json.loads(str(m.get("content") or "{}")).get("returncode", 0)
                except ValueError:
                    rc = 0
            events.append({"kind": "result", "id": str(m.get("tool_call_id") or ""), "tool": "",
                           "input_head": "", "is_error": bool(rc), "ts": extra.get("timestamp")})
            continue
        for call in m.get("tool_calls") or []:
            fn = call.get("function") or {}
            args = tool_input(fn.get("arguments"))
            cmd = args.get("command") if isinstance(args.get("command"), str) else ""
            name = str(fn.get("name") or "")
            events.append({"kind": "call", "id": str(call.get("id") or ""), "tool": name,
                           "input_head": head80(cmd or args), "is_error": None,
                           "ts": (extra.get("response") or {}).get("created"),
                           "hash": input_hash(name, args), "command": cmd[:CMD_MAX]})
    return events, len(msgs), usage


def record_spend(spec: dict, usage: dict) -> float | None:
    """A running mini run's spend so far, priced exactly as its exit books it (harness_spend at the
    chat provider's price row): None when its record holds no usage yet, so nothing is guessed."""
    if not usage or not usage.get("calls"):
        return None
    from . import benchrun
    from .providers import get_provider
    prov = get_provider(spec.get("provider") or "chat").use(spec.get("model") or "", spec.get("prices"))
    return float(benchrun.harness_spend(prov, usage)["cost_usd"])


def result_line(stats: dict | None, prov) -> str:
    """A Claude-stream `result` line carrying the bridge's usage, which the provider's parser reads as the run's
    tokens (a mini-swe-agent run streams none): '' when the bridge counted no call."""
    from . import benchrun
    usage = (stats or {}).get("usage") or {}
    if not usage.get("calls"):
        return ""
    got = benchrun.harness_spend(prov, usage)
    return json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "",
                       "total_cost_usd": got["cost_usd"],
                       "usage": {"input_tokens": got["input_tokens"], "output_tokens": got["output_tokens"],
                                 "cache_read_input_tokens": got["cache_read_tokens"],
                                 "cache_creation_input_tokens": int(usage.get("cache_write_tokens") or 0)},
                       "ttp_calls": int(usage["calls"]), "ttp_context_tokens_per_call": context_per_call(usage)}) + "\n"


def write_result(out_path, stats: dict | None, prov) -> None:
    """Append result_line() to the run's output (never raises: the run's accounting must not fail its end)."""
    try:
        line = result_line(stats, prov)
        if line:
            with open(out_path, "ab") as f:
                f.write(line.encode())
    except Exception:
        pass

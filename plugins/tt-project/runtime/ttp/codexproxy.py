# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0
"""The Codex bench harness's proxy: Codex speaks the Responses API, the run's bridge speaks Chat Completions.

A Codex arm (benchrun HARNESSES `codex`) runs `codex exec` in the cell with a custom model provider whose
base URL is a LiteLLM proxy. The runner starts that proxy (a child process of the runner, loopback only,
stopped with the run) in front of the run's bridge (chatbridge / claudebridge, so its capacity retries, its
server-wait records and its effort fields apply to Codex arms exactly as to the others). The cell reaches
the proxy only through its socket relay: the cell has no network of its own.

    codex (cell) -> relay socket -> LiteLLM proxy (127.0.0.1) -> bridge /v1 relay (127.0.0.1) -> model

The proxy's hook (HOOK_SOURCE, written next to its config) does what Codex needs from a Chat Completions
upstream: it normalises the reasoning effort Codex sends (the bridge then sets the model's own effort on every
call), drops `client_metadata` from the request, and skips stream events that carry no data. The proxy has
no key: it holds the run's bridge token, never the endpoint's key. Nothing here names a host, an endpoint or
a person; the versions are the arm's settings (`harness_version`, `proxy_version`).
"""
from __future__ import annotations

import json
import os
import shlex
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

from .project import durable_write, open_append

PROFILE = "bench"
PROVIDER = "bench"
EFFORTS = ("minimal", "low", "medium", "high")
TOKEN_ENV = "TTP_BRIDGE_TOKEN"   # the run's bridge token: the proxy's own master key and its upstream key

HOOK_SOURCE = '''\
"""LiteLLM hook for the Codex bench arm (written by ttp codexproxy)."""
from litellm.integrations.custom_logger import CustomLogger

EFFORTS = ("minimal", "low", "medium", "high")
DROPPED = ("client_metadata",)


def normalize(data: dict) -> dict:
    for key in DROPPED:
        data.pop(key, None)
    reasoning = data.get("reasoning")
    effort = data.get("reasoning_effort")
    if isinstance(reasoning, dict) and reasoning.get("effort") and not effort:
        effort = reasoning["effort"]
    data.pop("reasoning", None)
    data.pop("reasoning_effort", None)
    if effort in EFFORTS:
        data["reasoning_effort"] = effort
    elif effort in ("xhigh", "max"):
        data["reasoning_effort"] = "high"
    return data


def has_data(item) -> bool:
    if item is None or item == "" or item == b"":
        return False
    data = getattr(item, "data", item)
    if isinstance(data, dict):
        return bool(data)
    return data not in (None, "", b"")


class Hook(CustomLogger):
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        return normalize(data)

    async def async_post_call_streaming_iterator_hook(self, user_api_key_dict, response, request_data):
        async for item in response:
            if has_data(item):
                yield item


proxy_handler_instance = Hook()
'''


def config_yaml(model: str, upstream_url: str) -> str:
    """The proxy's config: one model, served as `model`, whose upstream is the bridge's /v1 relay. The run
    token reaches it by environment (TOKEN_ENV), never a file."""
    q = json.dumps
    return (
        "model_list:\n"
        f"  - model_name: {q(model)}\n"
        "    litellm_params:\n"
        f"      model: {q('openai/' + model)}\n"
        f"      api_base: {q(upstream_url)}\n"
        "      use_chat_completions_api: true\n"
        f"      api_key: os.environ/{TOKEN_ENV}\n"
        "litellm_settings:\n"
        "  drop_params: true\n"
        "  callbacks: [\"hook.proxy_handler_instance\"]\n"
        "  telemetry: false\n"
        "general_settings:\n"
        "  disable_spend_logs: true\n")


def _die_with_parent() -> None:
    """The proxy ends if the runner that started it dies (Linux parent-death signal)."""
    try:
        import ctypes
        ctypes.CDLL(None).prctl(1, 15)
    except (OSError, AttributeError):
        pass


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start(run: Path, install: dict, model: str, upstream_url: str, token: str, wait_s: float = 120.0):
    """Start the proxy for `run` on a free loopback port: (Popen, port). Stopped by the caller (stop())."""
    run = Path(run)
    folder = run / "codexproxy"
    folder.mkdir(parents=True, exist_ok=True)
    durable_write(folder / "hook.py", HOOK_SOURCE)
    durable_write(folder / "config.yaml", config_yaml(model, upstream_url))
    port = free_port()
    env = {"PATH": os.environ.get("PATH", ""), "HOME": str(folder), "LITELLM_TELEMETRY": "False",
           "LITELLM_LOG": "ERROR", "PYTHONPATH": str(folder), "NO_PROXY": "127.0.0.1,localhost",
           "LITELLM_MODE": "PRODUCTION", TOKEN_ENV: token, "LITELLM_MASTER_KEY": token, "DISABLE_SCHEMA_UPDATE": "true", "USE_PRISMA_MIGRATE": "false"}
    log = open_append(run / "codexproxy.log", "ab")
    # own session: the proxy serves the codex run for its whole life and is stopped by the caller (stop())
    proc = subprocess.Popen([install["bin"], "--config", str(folder / "config.yaml"), "--host", "127.0.0.1",
                             "--port", str(port)], cwd=str(folder), env=env, stdout=log, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, start_new_session=True,
                            preexec_fn=_die_with_parent)
    log.close()
    end = time.time() + wait_s
    while time.time() < end:
        if proc.poll() is not None:
            raise OSError(f"the codex proxy exited ({proc.returncode}): see {run / 'codexproxy.log'}")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health/liveliness", timeout=2) as r:
                if r.status == 200:
                    return proc, port
        except OSError:
            time.sleep(0.5)
    stop(proc)
    raise OSError(f"the codex proxy did not come up in {wait_s:.0f}s: see {run / 'codexproxy.log'}")


def stop(proc) -> None:
    """End the proxy this run started (its own process group; never by name)."""
    if proc is None or proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, 15)
        proc.wait(timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        try:
            os.killpg(proc.pid, 9)
            proc.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            pass


def codex_config(model: str, port: str | int, effort: str = "high") -> dict[str, str]:
    """The cell's CODEX_HOME files (name -> text): config.toml names the proxy as a model provider (Responses
    API, the run token as its key); `<profile>.config.toml` is the bench profile (Codex 0.169 profiles live in
    their own file): the model, approvals and Codex's own sandbox off (the cell is the sandbox), no web search,
    no telemetry. `port` may be a shell reference (SCRIPT_PORT) that the launch script expands."""
    return {
        "config.toml": (
            f'[model_providers.{PROVIDER}]\n'
            'name = "bench"\n'
            f'base_url = "http://127.0.0.1:{port}/v1"\n'
            'env_key = "OPENAI_API_KEY"\n'
            'wire_api = "responses"\n'
            '\n[analytics]\nenabled = false\n[feedback]\nenabled = false\n'),
        f"{PROFILE}.config.toml": (
            f'model = {json.dumps(model)}\n'
            f'model_provider = "{PROVIDER}"\n'
            f'model_reasoning_effort = {json.dumps(effort if effort in EFFORTS else "high")}\n'
            'approval_policy = "never"\n'
            'sandbox_mode = "danger-full-access"\n'
            'web_search = "disabled"\n')}


def argv(binary: str, model: str, workdir: str) -> list[str]:
    """`codex exec` with the bench profile; the prompt comes on stdin."""
    return [binary, "exec", "-p", PROFILE, "-m", model, "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox", "--json", "-C", workdir, "-"]


SCRIPT_PORT = "${TTP_RUN_PROXY_PORT}"   # the cell's relay port for the proxy, set by the runner


def script(binary: str, model: str, workdir: str, codex_home: str) -> str:
    """The arm's launch script body (harness.sh): CODEX_HOME in the cell's scratch, no login; its config is
    written at launch, as the proxy's port is known only once the runner has started it."""
    lines = [f"export CODEX_HOME={shlex.quote(codex_home)} CODEX_DISABLE_TELEMETRY=1", 'mkdir -p "$CODEX_HOME"']
    for name, text in codex_config(model, SCRIPT_PORT).items():
        lines.append(f'cat > "$CODEX_HOME/{name}" <<TTP_EOF\n{text}TTP_EOF')
    lines.append(f"exec {' '.join(shlex.quote(a) for a in argv(binary, model, workdir))}")
    return "\n".join(lines) + "\n"

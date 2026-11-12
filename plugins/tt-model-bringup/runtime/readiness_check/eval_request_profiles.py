# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Load the eval request profiles shared by the eval-readiness probes.

``eval_request_profiles.json`` (next to this file) lists what the eval
harnesses send. Probes read a profile by name::

    from readiness_check.eval_request_profiles import get_profile
    profile = get_profile("lm_eval_reasoning", hf_model="org/model")
    request = profile["request"]

A profile with ``request_from: hf_generation_config`` is filled from the
checkpoint's ``generation_config.json`` at run time, the same keys vLLM
applies to requests that omit them.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Optional

PROFILES_PATH = Path(__file__).with_name("eval_request_profiles.json")

# vLLM's generation-config defaults: HF key -> request key.
GENERATION_CONFIG_KEYS = {
    "temperature": "temperature",
    "top_p": "top_p",
    "top_k": "top_k",
    "min_p": "min_p",
    "repetition_penalty": "repetition_penalty",
    "max_new_tokens": "max_tokens",
}

BUILTIN_PROFILES: dict[str, Any] = {
    "profiles": [
        {"name": "plain", "source": "built-in fallback", "chat": True, "thinking": None, "request": {"temperature": 0.0}},
        {
            "name": "lm_eval_reasoning",
            "source": "built-in fallback (lm-eval r1_gpqa_diamond as TTI runs it)",
            "chat": True,
            "thinking": True,
            "request": {"temperature": 1.0, "top_p": 0.95, "top_k": 40, "seed": 42, "n": 1, "max_tokens": 32768},
        },
    ]
}


class ProfileError(Exception):
    pass


def load_profiles(path: Optional[Path | str] = None) -> list[dict]:
    """Profiles from ``path`` (default: the packaged file); the built-in set when the file is missing."""
    source = Path(path) if path else PROFILES_PATH
    if source.is_file():
        data = json.loads(source.read_text())
    elif path:
        raise ProfileError(f"profiles file {source} not found")
    else:
        data = copy.deepcopy(BUILTIN_PROFILES)
    profiles = data.get("profiles") if isinstance(data, dict) else None
    if not isinstance(profiles, list) or not profiles:
        raise ProfileError(f"{source}: 'profiles' must be a non-empty list")
    names = set()
    for p in profiles:
        if not isinstance(p, dict) or not p.get("name") or not isinstance(p.get("request"), dict):
            raise ProfileError(f"{source}: each profile needs a name and a request object")
        if not p.get("source"):
            raise ProfileError(f"{source}: profile {p['name']} has no source")
        if p["name"] in names:
            raise ProfileError(f"{source}: profile {p['name']} is repeated")
        names.add(p["name"])
    return profiles


def generation_config_defaults(hf_model: str, revision: Optional[str] = None) -> Optional[dict]:
    """Sampling defaults from the checkpoint's generation_config.json, or None if it has none.

    ``hf_model`` may be a local checkpoint directory or a Hub id (read from the
    local HF cache or downloaded with huggingface_hub).
    """
    local = Path(hf_model) / "generation_config.json"
    raw: Optional[dict] = None
    if local.is_file():
        raw = json.loads(local.read_text())
    else:
        try:
            from huggingface_hub import hf_hub_download

            raw = json.loads(Path(hf_hub_download(hf_model, "generation_config.json", revision=revision)).read_text())
        except Exception:  # noqa: BLE001 - no file, no network or no hub: the caller reports a skip
            return None
    out = {req: raw[hf] for hf, req in GENERATION_CONFIG_KEYS.items() if raw.get(hf) is not None}
    return out


def resolve_profile(profile: dict, hf_model: Optional[str] = None, revision: Optional[str] = None) -> dict:
    """Copy of the profile with a run-time request filled in. Adds ``unresolved`` with a reason if it cannot be."""
    out = copy.deepcopy(profile)
    if out.get("request_from") == "hf_generation_config":
        defaults = generation_config_defaults(hf_model, revision) if hf_model else None
        if defaults is None:
            out["unresolved"] = "generation_config.json not readable for this model"
        elif not any(k != "max_tokens" for k in defaults):
            out["unresolved"] = "generation_config.json sets no sampling defaults; the harness profiles cover the requests"
        out["request"] = {**(defaults or {}), **out.get("request", {})}
    return out


def get_profile(
    name: str,
    *,
    hf_model: Optional[str] = None,
    revision: Optional[str] = None,
    path: Optional[Path | str] = None,
) -> dict:
    for profile in load_profiles(path):
        if profile["name"] == name:
            return resolve_profile(profile, hf_model, revision)
    raise ProfileError(f"no profile named {name!r}")

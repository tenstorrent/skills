#!/usr/bin/env python3
"""Launch the generic computer-vision model-bringup workflow."""

from __future__ import annotations

import argparse
import os
import pathlib
import subprocess
import sys
from typing import Any

from check_model_contract import (
    ContractError,
    load_contract,
    validate_reference_checkout,
    validate_weight_identity,
    vision_prompt_plan,
)


ROOT = pathlib.Path(__file__).resolve().parents[1]
PROMPTS = ROOT / "prompts" / "model_bringup_cv"


def build_command(
    contract_path: pathlib.Path,
    contract: dict[str, Any],
    repo: pathlib.Path,
    runner_args: list[str],
) -> list[str]:
    prompt_paths = [PROMPTS / name for name in vision_prompt_plan(contract)]
    missing = [path for path in prompt_paths if not path.is_file()]
    if missing:
        raise ContractError(f"missing CV prompt: {missing[0]}")

    model = contract["model"]
    paths = contract["paths"]
    replacements = {
        "MODEL_CONTRACT": str(contract_path),
        "MODEL_DIR": paths["target_dir"],
        "MODEL_NAME": model["name"],
        "UPSTREAM_REPO": model["source_repository"],
        "UPSTREAM_REVISION": model["source_revision"],
        "TASK_FAMILY": model["task"],
    }
    command = [sys.executable, str(ROOT / "scripts" / "multigoal"), *map(str, prompt_paths), "--repo", str(repo)]
    for key, value in replacements.items():
        command.extend(["--replace", f"{key}={value}"])
    command.extend(runner_args)
    return command


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Plan and run generic CV model bring-up goals.")
    parser.add_argument("--contract", required=True, type=pathlib.Path)
    parser.add_argument("--repo", type=pathlib.Path, default=pathlib.Path.cwd())
    parser.add_argument("--print-plan", action="store_true")
    parser.add_argument("runner_args", nargs=argparse.REMAINDER, help="Arguments after -- go to multigoal.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo = args.repo.resolve()
    contract_path = args.contract if args.contract.is_absolute() else (repo / args.contract)
    runner_args = args.runner_args[1:] if args.runner_args[:1] == ["--"] else args.runner_args
    try:
        contract = load_contract(contract_path)
        validate_reference_checkout(contract, repo)
        validate_weight_identity(contract, repo)
        command = build_command(contract_path.resolve(), contract, repo, runner_args)
    except ContractError as exc:
        print(f"CV workflow rejected: {exc}", file=sys.stderr)
        return 2

    if args.print_plan:
        for prompt in vision_prompt_plan(contract):
            print(prompt)
        return 0
    return subprocess.call(command, env=os.environ.copy())


if __name__ == "__main__":
    raise SystemExit(main())

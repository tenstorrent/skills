#!/usr/bin/env python3
"""Validate and plan model-bringup contracts."""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import subprocess
import sys
from typing import Any


CATEGORIES = {"text", "vision", "multimodal", "other"}
TASKS = {
    "text-generation",
    "classification",
    "detection",
    "segmentation",
    "bev",
    "planning",
    "diffusion",
    "vision-language",
    "other",
}
PROFILES = {"functional", "optimized", "release"}
TOPOLOGIES = {"single-chip", "auto", "multichip"}
SERVING_MODES = {"none", "task-api", "vllm"}
STATE_MODES = {"stateless", "temporal", "iterative", "kv-cache"}
IMMUTABLE_REVISION = re.compile(r"^[0-9a-f]{40}$")
WEIGHTS_DIGEST = re.compile(r"^(?:sha256:[0-9a-f]{64}|manifest:[A-Za-z0-9_./-]+)$")
TOP_LEVEL_KEYS = {"schema_version", "model", "execution", "paths", "validation"}
MODEL_KEYS = {
    "name",
    "category",
    "task",
    "source_repository",
    "source_revision",
    "weights",
    "weights_revision",
    "weights_digest",
    "weights_file",
}
EXECUTION_KEYS = {"profile", "topology", "serving", "state", "multichip_reason"}
PATH_KEYS = {"reference_root", "weights_root", "target_dir", "write_root", "forbidden_read_roots"}
VALIDATION_KEYS = {
    "reference_framework",
    "raw_outputs",
    "metrics",
    "dataset",
    "preprocessing",
    "postprocessing",
    "risks",
    "acceptance",
}
ACCEPTANCE_KEYS = {"metric", "operator", "value", "unit"}
ACCEPTANCE_METRICS = {
    "pcc",
    "cosine_similarity",
    "max_abs_error",
    "mean_abs_error",
    "absolute_delta",
    "relative_delta",
    "exact",
    "task_score",
}
HIGHER_IS_BETTER = {"pcc", "cosine_similarity", "task_score"}
LOWER_IS_BETTER = {"max_abs_error", "mean_abs_error", "absolute_delta", "relative_delta"}


class ContractError(ValueError):
    """Raised when a model contract is incomplete or contradictory."""


def _reject_unknown(parent: dict[str, Any], allowed: set[str], prefix: str) -> None:
    unknown = sorted(set(parent) - allowed)
    if unknown:
        raise ContractError(f"{prefix} has unknown keys: {', '.join(unknown)}")


def _mapping(parent: dict[str, Any], key: str) -> dict[str, Any]:
    value = parent.get(key)
    if not isinstance(value, dict):
        raise ContractError(f"{key} must be an object")
    return value


def _string(parent: dict[str, Any], key: str, prefix: str) -> str:
    value = parent.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{prefix}.{key} must be a non-empty string")
    return value.strip()


def _choice(parent: dict[str, Any], key: str, prefix: str, choices: set[str]) -> str:
    value = _string(parent, key, prefix)
    if value not in choices:
        options = ", ".join(sorted(choices))
        raise ContractError(f"{prefix}.{key} must be one of: {options}")
    return value


def _strings(parent: dict[str, Any], key: str, prefix: str, *, required: bool) -> list[str]:
    value = parent.get(key)
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise ContractError(f"{prefix}.{key} must be a list of non-empty strings")
    if required and not value:
        raise ContractError(f"{prefix}.{key} must not be empty")
    return [item.strip() for item in value]


def _relative_path(value: str, field: str) -> pathlib.PurePosixPath:
    path = pathlib.PurePosixPath(value)
    if path.is_absolute() or value in {"", "."} or ".." in path.parts:
        raise ContractError(f"{field} must be a safe repository-relative path")
    return path


def _is_within(path: pathlib.PurePosixPath, root: pathlib.PurePosixPath) -> bool:
    return path == root or root in path.parents


def _acceptance(parent: dict[str, Any], expected: set[str]) -> None:
    value = parent.get("acceptance")
    if not isinstance(value, dict) or not value:
        raise ContractError("validation.acceptance must be a non-empty object")
    missing = sorted(expected - set(value))
    if missing:
        raise ContractError(f"validation.acceptance is missing: {', '.join(missing)}")
    unknown = sorted(set(value) - expected)
    if unknown:
        raise ContractError(f"validation.acceptance has unknown outputs or metrics: {', '.join(unknown)}")
    for name, rule in value.items():
        if not isinstance(rule, dict):
            raise ContractError(f"validation.acceptance.{name} must be an object")
        _reject_unknown(rule, ACCEPTANCE_KEYS, f"validation.acceptance.{name}")
        metric = _choice(rule, "metric", f"validation.acceptance.{name}", ACCEPTANCE_METRICS)
        operator = _choice(rule, "operator", f"validation.acceptance.{name}", {"<=", "==", ">="})
        threshold = rule.get("value")
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
            raise ContractError(f"validation.acceptance.{name}.value must be numeric")
        _string(rule, "unit", f"validation.acceptance.{name}")
        if metric in HIGHER_IS_BETTER and operator != ">=":
            raise ContractError(f"validation.acceptance.{name} metric {metric} requires operator >=")
        if metric in LOWER_IS_BETTER and operator != "<=":
            raise ContractError(f"validation.acceptance.{name} metric {metric} requires operator <=")
        if metric == "exact" and (operator != "==" or threshold not in {0, 1}):
            raise ContractError(f"validation.acceptance.{name} exact metric requires == 0 or == 1")
        if metric in {"pcc", "cosine_similarity"} and not 0 <= threshold <= 1:
            raise ContractError(f"validation.acceptance.{name} {metric} threshold must be between 0 and 1")
        if metric in LOWER_IS_BETTER and threshold < 0:
            raise ContractError(f"validation.acceptance.{name} error or delta threshold must be non-negative")


def validate_contract(data: dict[str, Any]) -> dict[str, Any]:
    _reject_unknown(data, TOP_LEVEL_KEYS, "contract")
    if data.get("schema_version") != 1:
        raise ContractError("schema_version must equal 1")

    model = _mapping(data, "model")
    execution = _mapping(data, "execution")
    paths = _mapping(data, "paths")
    validation = _mapping(data, "validation")
    _reject_unknown(model, MODEL_KEYS, "model")
    _reject_unknown(execution, EXECUTION_KEYS, "execution")
    _reject_unknown(paths, PATH_KEYS, "paths")
    _reject_unknown(validation, VALIDATION_KEYS, "validation")

    _string(model, "name", "model")
    category = _choice(model, "category", "model", CATEGORIES)
    task = _choice(model, "task", "model", TASKS)
    _string(model, "source_repository", "model")
    revision = _string(model, "source_revision", "model")
    if not IMMUTABLE_REVISION.fullmatch(revision):
        raise ContractError("model.source_revision must be a full 40-character Git commit")
    _string(model, "weights", "model")
    _string(model, "weights_revision", "model")
    weights_digest = _string(model, "weights_digest", "model")
    if not WEIGHTS_DIGEST.fullmatch(weights_digest):
        raise ContractError("model.weights_digest must be sha256:<64 hex> or manifest:<relative path>")
    if weights_digest.startswith("manifest:"):
        _relative_path(weights_digest.removeprefix("manifest:"), "model.weights_digest manifest")
        if "weights_file" in model:
            raise ContractError("model.weights_file is only valid with a direct sha256 digest")
    else:
        _relative_path(_string(model, "weights_file", "model"), "model.weights_file")

    profile = _choice(execution, "profile", "execution", PROFILES)
    topology = _choice(execution, "topology", "execution", TOPOLOGIES)
    serving = _choice(execution, "serving", "execution", SERVING_MODES)
    state = _choice(execution, "state", "execution", STATE_MODES)

    if category == "vision" and task in {"text-generation", "vision-language"}:
        raise ContractError(f"vision models cannot use task {task}")
    if category == "multimodal" and task != "vision-language":
        raise ContractError("multimodal models must use task vision-language")
    if category == "text" and task != "text-generation":
        raise ContractError("text models must use task text-generation")
    if category == "vision" and serving == "vllm":
        raise ContractError("pure vision models cannot use vllm serving")
    if profile == "functional" and serving != "none":
        raise ContractError("functional profile requires serving=none")
    if state == "kv-cache" and category not in {"text", "multimodal"}:
        raise ContractError("kv-cache state requires text or multimodal category")
    if topology == "multichip":
        _string(execution, "multichip_reason", "execution")

    target_dir = _relative_path(_string(paths, "target_dir", "paths"), "paths.target_dir")
    write_root = _relative_path(_string(paths, "write_root", "paths"), "paths.write_root")
    if not _is_within(target_dir, write_root):
        raise ContractError("paths.target_dir must be inside paths.write_root")
    _string(paths, "reference_root", "paths")
    _string(paths, "weights_root", "paths")
    forbidden = _strings(paths, "forbidden_read_roots", "paths", required=False)
    for index, value in enumerate(forbidden):
        _relative_path(value, f"paths.forbidden_read_roots[{index}]")

    _string(validation, "reference_framework", "validation")
    raw_outputs = _strings(validation, "raw_outputs", "validation", required=True)
    metrics = _strings(validation, "metrics", "validation", required=True)
    _string(validation, "dataset", "validation")
    _strings(validation, "preprocessing", "validation", required=True)
    _strings(validation, "postprocessing", "validation", required=False)
    _strings(validation, "risks", "validation", required=False)
    _acceptance(validation, set(raw_outputs + metrics))

    return data


def load_contract(path: pathlib.Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ContractError(f"contract does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ContractError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ContractError("contract root must be an object")
    return validate_contract(data)


def validate_reference_checkout(data: dict[str, Any], repo: pathlib.Path) -> pathlib.Path:
    data = validate_contract(data)
    configured = pathlib.Path(data["paths"]["reference_root"])
    reference_root = configured if configured.is_absolute() else repo / configured
    reference_root = reference_root.resolve()
    if not reference_root.is_dir():
        raise ContractError(f"paths.reference_root does not exist: {reference_root}")

    def git(*args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(reference_root), *args],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip()
            raise ContractError(f"reference checkout Git check failed: {detail}")
        return result.stdout.strip()

    revision = data["model"]["source_revision"]
    git("cat-file", "-e", f"{revision}^{{commit}}")
    head = git("rev-parse", "HEAD")
    if head != revision:
        raise ContractError(f"reference checkout HEAD {head} does not match source_revision {revision}")
    origin = git("remote", "get-url", "origin")

    def normalize_repository(value: str) -> str:
        normalized = value.strip().removesuffix(".git").rstrip("/")
        if normalized.startswith("git@") and ":" in normalized:
            host, path = normalized.removeprefix("git@").split(":", 1)
            normalized = f"{host}/{path}"
        normalized = re.sub(r"^[a-z]+://", "", normalized)
        return normalized.lower()

    expected = data["model"]["source_repository"]
    if normalize_repository(origin) != normalize_repository(expected):
        raise ContractError(f"reference checkout origin {origin} does not match source_repository {expected}")
    return reference_root


def validate_weight_identity(data: dict[str, Any], repo: pathlib.Path) -> pathlib.Path | None:
    data = validate_contract(data)
    digest = data["model"]["weights_digest"]
    configured_root = pathlib.Path(data["paths"]["weights_root"])
    weights_root = configured_root if configured_root.is_absolute() else repo / configured_root
    weights_root = weights_root.resolve()
    if not weights_root.is_dir():
        raise ContractError(f"paths.weights_root does not exist: {weights_root}")

    def file_sha256(path: pathlib.Path) -> str:
        digest_builder = hashlib.sha256()
        try:
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    digest_builder.update(chunk)
        except FileNotFoundError as exc:
            raise ContractError(f"weight artifact does not exist: {path}") from exc
        return digest_builder.hexdigest()

    if digest.startswith("sha256:"):
        weight_file = weights_root / data["model"]["weights_file"]
        expected = digest.removeprefix("sha256:")
        actual = file_sha256(weight_file)
        if actual != expected:
            raise ContractError(f"weight artifact SHA-256 mismatch: {weight_file}")
        return weight_file
    manifest_path = repo / digest.removeprefix("manifest:")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ContractError(f"weight digest manifest does not exist: {manifest_path}") from exc
    except json.JSONDecodeError as exc:
        raise ContractError(f"invalid weight digest manifest JSON: {exc}") from exc
    artifacts = manifest.get("artifacts") if isinstance(manifest, dict) else None
    if not isinstance(artifacts, list) or not artifacts:
        raise ContractError("weight digest manifest must contain a non-empty artifacts list")
    for index, artifact in enumerate(artifacts):
        if not isinstance(artifact, dict) or set(artifact) != {"path", "sha256"}:
            raise ContractError(f"weight digest artifact {index} must contain only path and sha256")
        path = artifact.get("path")
        sha256 = artifact.get("sha256")
        if not isinstance(path, str) or not path or not isinstance(sha256, str):
            raise ContractError(f"weight digest artifact {index} has invalid fields")
        _relative_path(path, f"weight digest artifact {index} path")
        if not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise ContractError(f"weight digest artifact {index} sha256 must contain 64 lowercase hex characters")
        if file_sha256(weights_root / path) != sha256:
            raise ContractError(f"weight digest artifact {index} SHA-256 mismatch: {path}")
    return manifest_path


def vision_prompt_plan(data: dict[str, Any]) -> list[str]:
    data = validate_contract(data)
    model = data["model"]
    execution = data["execution"]
    category = model["category"]
    if category == "text":
        raise ContractError("text models must use model_bringup_multigoal")
    if category == "multimodal":
        raise ContractError("multimodal models require the model-bringup hybrid workflow")
    if category != "vision":
        raise ContractError("CV multigoal requires model.category=vision")

    prompts = [
        "01-reference-contract.txt",
        "02-functional-modules.txt",
        "03-functional-model.txt",
    ]
    if execution["state"] in {"temporal", "iterative"}:
        prompts.append("04-stateful-sequence.txt")
    prompts.append("05-task-eval.txt")

    if execution["profile"] in {"optimized", "release"}:
        prompts.extend(["06-fused-model.txt", "07-optimized-model.txt"])
    if execution["topology"] == "multichip":
        prompts.append("08-multichip-model.txt")
    if execution["serving"] == "task-api":
        prompts.append("09-task-api.txt")
    if execution["profile"] == "release":
        prompts.append("10-release-evidence.txt")
    return prompts


def workflow_summary(data: dict[str, Any]) -> list[str]:
    data = validate_contract(data)
    category = data["model"]["category"]
    if category == "vision":
        return ["workflow=model_bringup_cv", *vision_prompt_plan(data)]
    if category == "text":
        return ["workflow=model_bringup_multigoal"]
    if category == "multimodal":
        return ["workflow=model_bringup_hybrid", "vision_encoder=functional-vision", "decoder=text-workflow"]
    return ["workflow=unsupported"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate a TT model-bringup contract.")
    parser.add_argument("contract", type=pathlib.Path)
    parser.add_argument("--print-plan", action="store_true")
    parser.add_argument("--repo", type=pathlib.Path, default=pathlib.Path.cwd())
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        contract = load_contract(args.contract)
        validate_reference_checkout(contract, args.repo.resolve())
        validate_weight_identity(contract, args.repo.resolve())
        plan = workflow_summary(contract) if args.print_plan else []
    except ContractError as exc:
        print(f"model contract invalid: {exc}", file=sys.stderr)
        return 2
    print(f"model contract valid: {args.contract}")
    for prompt in plan:
        print(prompt)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import importlib.util
import hashlib
import json
import pathlib
import subprocess
import sys

import pytest


ROOT = pathlib.Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "tt-model-bringup"
SCRIPTS = PLUGIN / "scripts"
sys.path.insert(0, str(SCRIPTS))


def load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


contract_module = load("model_contract", SCRIPTS / "check_model_contract.py")
cv_runner = load("cv_multigoal", SCRIPTS / "cv_multigoal.py")


def valid_contract(**execution_overrides):
    execution = {
        "profile": "functional",
        "topology": "single-chip",
        "serving": "none",
        "state": "stateless",
        **execution_overrides,
    }
    return {
        "schema_version": 2,
        "model": {
            "name": "example-vision-model",
            "category": "vision",
            "task": "classification",
            "source_repository": "https://github.com/example/model",
            "source_revision": "a" * 40,
            "weights": "example/model",
            "weights_revision": "IMAGENET1K_V2",
            "weights_digest": "sha256:" + "b" * 64,
            "weights_file": "resnet50.pth",
            "config_scope": {
                "mode": "pinned",
                "configs": ["default"],
                "selection_source": "explicit-user-choice",
            },
        },
        "execution": execution,
        "paths": {
            "reference_root": "/tmp/upstream-model",
            "weights_root": "/tmp/model-weights",
            "target_dir": "models/autoports/example/run-1",
            "write_root": "models/autoports/example/run-1",
            "forbidden_read_roots": ["models/experimental/example"],
        },
        "validation": {
            "reference_framework": "pytorch",
            "raw_outputs": ["logits"],
            "metrics": ["top-1", "top-5"],
            "dataset": "ImageNet-1K",
            "preprocessing": ["resize 256", "center crop 224", "normalize ImageNet"],
            "postprocessing": ["argmax logits"],
            "risks": ["residual downsample shape"],
            "acceptance": {
                "logits": {"metric": "pcc", "operator": ">=", "value": 0.99, "unit": "ratio"},
                "top-1": {
                    "metric": "absolute_delta", "operator": "<=", "value": 0.01, "unit": "fraction"
                },
                "top-5": {
                    "metric": "absolute_delta", "operator": "<=", "value": 0.01, "unit": "fraction"
                },
            },
        },
        "assets": [
            {
                "kind": "source",
                "name": "example source",
                "source": "https://github.com/example/model",
                "license": "Apache-2.0",
                "license_url": "https://github.com/example/model/blob/main/LICENSE",
                "commercial_use": "allowed",
                "redistribution": "allowed",
                "included_in_package": False,
                "notes": [],
            },
            {
                "kind": "weights",
                "name": "example weights",
                "source": "example/model",
                "license": "unknown",
                "license_url": None,
                "commercial_use": "unknown",
                "redistribution": "unknown",
                "included_in_package": False,
                "notes": ["Resolve before redistribution"],
            },
            {
                "kind": "dataset",
                "name": "ImageNet-1K",
                "source": "https://www.image-net.org/",
                "license": "ImageNet terms",
                "license_url": "https://www.image-net.org/download.php",
                "commercial_use": "restricted",
                "redistribution": "restricted",
                "included_in_package": False,
                "notes": ["Dataset is not redistributed"],
            },
        ],
    }


def legacy_contract():
    contract = valid_contract()
    contract["schema_version"] = 1
    del contract["model"]["config_scope"]
    del contract["assets"]
    return contract


def test_schema_one_remains_valid_for_existing_runs():
    assert contract_module.validate_contract(legacy_contract())["schema_version"] == 1


def test_schema_two_requires_explicit_config_scope():
    contract = valid_contract()
    del contract["model"]["config_scope"]
    with pytest.raises(contract_module.ContractError, match="config_scope"):
        contract_module.validate_contract(contract)


def test_pinned_config_scope_requires_exactly_one_config():
    contract = valid_contract()
    contract["model"]["config_scope"]["configs"] = ["small", "base"]
    with pytest.raises(contract_module.ContractError, match="exactly one"):
        contract_module.validate_contract(contract)


def test_generic_config_scope_requires_multiple_configs():
    contract = valid_contract()
    contract["model"]["config_scope"].update(mode="generic", configs=["tiny"])
    with pytest.raises(contract_module.ContractError, match="at least two"):
        contract_module.validate_contract(contract)


def test_generic_config_scope_accepts_multiple_configs():
    contract = valid_contract()
    contract["model"]["config_scope"].update(mode="generic", configs=["small", "base"])
    scope = contract_module.validate_contract(contract)["model"]["config_scope"]
    assert scope["mode"] == "generic"


def test_config_scope_requires_explicit_user_choice():
    contract = valid_contract()
    contract["model"]["config_scope"]["selection_source"] = "checkpoint-name"
    with pytest.raises(contract_module.ContractError, match="explicit-user-choice"):
        contract_module.validate_contract(contract)


def test_schema_two_requires_asset_disclosures():
    contract = valid_contract()
    del contract["assets"]
    with pytest.raises(contract_module.ContractError, match="assets"):
        contract_module.validate_contract(contract)


def test_assets_require_source_weights_and_dataset():
    contract = valid_contract()
    contract["assets"] = contract["assets"][:1]
    with pytest.raises(contract_module.ContractError, match="dataset"):
        contract_module.validate_contract(contract)


def test_assets_allow_explicit_unknown_license_policy():
    contract = valid_contract()
    weights = contract["assets"][1]
    weights.update(license="unknown", commercial_use="unknown", redistribution="unknown")
    assert contract_module.validate_contract(contract)["assets"][1] == weights


def test_assets_require_package_inclusion_disclosure():
    contract = valid_contract()
    del contract["assets"][0]["included_in_package"]
    with pytest.raises(contract_module.ContractError, match="included_in_package"):
        contract_module.validate_contract(contract)


def test_generated_readme_contract_requires_disclosures_and_structures():
    guide = (PLUGIN / "skills" / "functional-vision" / "references" / "generated-readme.md").read_text()
    assert "`Licensed Assets`" in guide
    assert "`PyTorch Model Structure`" in guide
    assert "torch_model_structure_<config>.txt" in guide
    assert "commercial-use" in guide


def test_functional_stateless_plan_stops_after_task_evaluation():
    plan = contract_module.vision_prompt_plan(valid_contract())
    assert plan == [
        "01-reference-contract.txt",
        "02-functional-modules.txt",
        "03-functional-model.txt",
        "05-task-eval.txt",
    ]


def test_temporal_plan_adds_sequence_gate():
    plan = contract_module.vision_prompt_plan(valid_contract(state="temporal"))
    assert "04-stateful-sequence.txt" in plan


def test_optimized_multichip_plan_is_explicit():
    contract = valid_contract(profile="optimized", topology="multichip")
    contract["execution"]["multichip_reason"] = "model weights exceed one device"
    plan = contract_module.vision_prompt_plan(contract)
    assert plan[-3:] == ["06-fused-model.txt", "07-optimized-model.txt", "08-multichip-model.txt"]


def test_release_task_api_plan_is_explicit():
    plan = contract_module.vision_prompt_plan(valid_contract(profile="release", serving="task-api"))
    assert plan[-2:] == ["09-task-api.txt", "10-release-evidence.txt"]


def test_functional_profile_rejects_serving():
    with pytest.raises(contract_module.ContractError, match="functional profile"):
        contract_module.validate_contract(valid_contract(serving="task-api"))


def test_pure_vision_rejects_vllm():
    contract = valid_contract(profile="release", serving="vllm")
    with pytest.raises(contract_module.ContractError, match="cannot use vllm"):
        contract_module.validate_contract(contract)


def test_target_must_stay_inside_write_root():
    contract = valid_contract()
    contract["paths"]["write_root"] = "models/autoports/other"
    with pytest.raises(contract_module.ContractError, match="inside paths.write_root"):
        contract_module.validate_contract(contract)


def test_unknown_contract_keys_are_rejected():
    contract = valid_contract()
    contract["validation"]["typo"] = "ignored"
    with pytest.raises(contract_module.ContractError, match="unknown keys"):
        contract_module.validate_contract(contract)


def test_acceptance_covers_every_output_and_metric():
    contract = valid_contract()
    del contract["validation"]["acceptance"]["top-5"]
    with pytest.raises(contract_module.ContractError, match="missing: top-5"):
        contract_module.validate_contract(contract)


def test_acceptance_rejects_wrong_direction_and_range():
    contract = valid_contract()
    contract["validation"]["acceptance"]["logits"].update(operator="<=", value=2.0)
    with pytest.raises(contract_module.ContractError, match="requires operator >="):
        contract_module.validate_contract(contract)


def test_multichip_requires_a_reason():
    contract = valid_contract(profile="optimized", topology="multichip")
    with pytest.raises(contract_module.ContractError, match="multichip_reason"):
        contract_module.validate_contract(contract)


def test_text_and_multimodal_models_use_other_workflows():
    for category, task, message in [
        ("text", "text-generation", "text models"),
        ("multimodal", "vision-language", "hybrid workflow"),
    ]:
        contract = valid_contract()
        contract["model"].update(category=category, task=task)
        with pytest.raises(contract_module.ContractError, match=message):
            contract_module.vision_prompt_plan(contract)


def test_general_plan_routes_text_and_multimodal_models():
    text = valid_contract()
    text["model"].update(category="text", task="text-generation")
    assert contract_module.workflow_summary(text) == ["workflow=model_bringup_multigoal"]

    multimodal = valid_contract()
    multimodal["model"].update(category="multimodal", task="vision-language")
    assert contract_module.workflow_summary(multimodal)[0] == "workflow=model_bringup_hybrid"


def test_cv_runner_builds_existing_prompt_paths(tmp_path):
    contract = valid_contract()
    contract_path = tmp_path / "contract.json"
    command = cv_runner.build_command(contract_path, contract, tmp_path, ["--dry-run"])
    assert command[-1] == "--dry-run"
    assert str(PLUGIN / "scripts" / "multigoal") in command
    assert all((PLUGIN / "prompts" / "model_bringup_cv" / name).is_file()
               for name in contract_module.vision_prompt_plan(contract))


def test_reference_checkout_must_match_pinned_commit(tmp_path):
    reference = tmp_path / "upstream"
    reference.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=reference, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=reference, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=reference, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", "https://github.com/example/model.git"], cwd=reference, check=True
    )
    (reference / "model.py").write_text("MODEL = True\n", encoding="utf-8")
    subprocess.run(["git", "add", "model.py"], cwd=reference, check=True)
    subprocess.run(["git", "commit", "-qm", "model"], cwd=reference, check=True)
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=reference, check=True, text=True, capture_output=True
    ).stdout.strip()
    contract = valid_contract()
    contract["model"]["source_revision"] = revision
    contract["paths"]["reference_root"] = str(reference)
    assert contract_module.validate_reference_checkout(contract, tmp_path) == reference

    contract["model"]["source_revision"] = "c" * 40
    with pytest.raises(contract_module.ContractError, match="Git check failed"):
        contract_module.validate_reference_checkout(contract, tmp_path)


def test_weight_digest_manifest_is_structured(tmp_path):
    contract = valid_contract()
    contract["model"]["weights_digest"] = "manifest:weights.json"
    del contract["model"]["weights_file"]
    weights_root = tmp_path / "weights"
    weights_root.mkdir()
    artifact = weights_root / "model.bin"
    artifact.write_bytes(b"weights")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    contract["paths"]["weights_root"] = str(weights_root)
    manifest = tmp_path / "weights.json"
    manifest.write_text(
        json.dumps({"artifacts": [{"path": "model.bin", "sha256": digest}]}),
        encoding="utf-8",
    )
    assert contract_module.validate_weight_identity(contract, tmp_path) == manifest

    manifest.write_text(json.dumps({"artifacts": [{"path": "model.bin", "sha256": "short"}]}))
    with pytest.raises(contract_module.ContractError, match="64 lowercase hex"):
        contract_module.validate_weight_identity(contract, tmp_path)


def test_direct_weight_digest_hashes_the_artifact(tmp_path):
    contract = valid_contract()
    weights_root = tmp_path / "weights"
    weights_root.mkdir()
    artifact = weights_root / "resnet50.pth"
    artifact.write_bytes(b"weights")
    contract["paths"]["weights_root"] = str(weights_root)
    contract["model"]["weights_digest"] = "sha256:" + hashlib.sha256(artifact.read_bytes()).hexdigest()
    assert contract_module.validate_weight_identity(contract, tmp_path) == artifact

    contract["model"]["weights_digest"] = "sha256:" + "e" * 64
    with pytest.raises(contract_module.ContractError, match="SHA-256 mismatch"):
        contract_module.validate_weight_identity(contract, tmp_path)


def test_cv_prompts_reference_packaged_skills():
    skill_names = {path.parent.name for path in (PLUGIN / "skills").glob("*/SKILL.md")}
    for prompt in (PLUGIN / "prompts" / "model_bringup_cv").glob("*.txt"):
        text = prompt.read_text(encoding="utf-8")
        assert text.startswith("/goal ")
        referenced = set(__import__("re").findall(r"\$([a-z][a-z0-9_-]+)", text))
        assert referenced <= skill_names, (prompt, referenced - skill_names)

"""Exercise the published decode adapter example without model dependencies."""
from __future__ import annotations

import importlib.util
import inspect
from itertools import product
from pathlib import Path
import re
import sys
from types import ModuleType
from unittest.mock import Mock

import pytest

PLUGIN = Path(__file__).resolve().parents[1] / "plugins/tt-model-bringup"
COMMANDS = (
    "reload_inputs",
    "reload_page_table",
    "reload_sampling_params",
    "reset_sampling_state",
)
# Full, page-table-only, and resident input modes. State reset needs full inputs.
PLANS = [
    dict(zip(COMMANDS, values))
    for values in product((False, True), repeat=4)
    if not (values[0] and values[1]) and (not values[3] or values[0])
]


def decode_inputs():
    return dict(
        tokens=object(), start_pos=object(), page_table=object(), kv_cache=object(),
        enable_trace=True, read_from_device=False,
    )


@pytest.fixture
def adapter_example():
    reference = PLUGIN / "skills/vllm-integration/references/decode-reload-contract.md"
    examples = re.findall(r"```python\n(.*?)\n```", reference.read_text(), re.DOTALL)
    namespace = {}
    for example in examples:
        exec(compile(example, str(reference), "exec"), namespace)
    generator = Mock()
    return namespace["DecodeAdapter"](generator), generator


@pytest.mark.parametrize("plan", PLANS)
@pytest.mark.parametrize("device_sampling", [False, True])
def test_example_forwards_commands_and_slot_state_once(adapter_example, plan, device_sampling):
    adapter, generator = adapter_example
    inputs = decode_inputs()
    remap = [1, 0, 2]
    optional = {"slot_remap": remap}
    if device_sampling:
        optional.update(sampling_params=object(), prompt_tokens=object(), output_tokens=object())

    result = adapter.decode_forward(**inputs, **plan, **optional)

    assert result is generator.decode_forward.return_value
    generator.decode_forward.assert_called_once_with(**inputs, **plan, **optional)
    assert generator.decode_forward.call_args.kwargs["slot_remap"] is remap


@pytest.mark.parametrize("missing", COMMANDS)
def test_example_requires_every_command(adapter_example, missing):
    adapter, generator = adapter_example
    plan = dict(zip(COMMANDS, (True, False, False, False)))
    del plan[missing]
    with pytest.raises(TypeError):
        adapter.decode_forward(**decode_inputs(), **plan)
    generator.decode_forward.assert_not_called()


def test_example_rejects_legacy_keyword_before_forward(adapter_example):
    adapter, generator = adapter_example
    plan = dict(zip(COMMANDS, (True, False, False, False)))
    with pytest.raises(TypeError, match="reset_batch"):
        adapter.decode_forward(**decode_inputs(), **plan, reset_batch=False)
    generator.decode_forward.assert_not_called()


@pytest.mark.parametrize("filename, class_name", [
    ("contract.py", "Generator"),
    ("contract_vllm.py", "VllmGeneratorAdapter"),
])
def test_python_contracts_accept_explicit_calls_and_require_commands(monkeypatch, filename, class_name):
    # The contracts need no tensor operations. Keep this test in the offline CI suite.
    with monkeypatch.context() as imports:
        imports.setitem(sys.modules, "torch", ModuleType("torch"))
        path = PLUGIN / "runtime/readiness_check" / filename
        spec = importlib.util.spec_from_file_location("decode_contract_test", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    signature = inspect.signature(getattr(module, class_name).decode_forward)
    for plan in PLANS:
        signature.bind(object(), **decode_inputs(), **plan, slot_remap=[0])
    for missing in COMMANDS:
        partial = dict(zip(COMMANDS, (True, False, False, False)))
        del partial[missing]
        with pytest.raises(TypeError):
            signature.bind(object(), **decode_inputs(), **partial)

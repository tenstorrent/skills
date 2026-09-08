# SPDX-FileCopyrightText: © 2026 Tenstorrent USA, Inc.
# SPDX-License-Identifier: Apache-2.0

"""Readiness contracts and runners. Heavy dependencies load only when requested."""
from importlib import import_module

_EXPORTS = {'BUILD_GENERATOR_FUNCTION_NAME': 'contract', 'GENERATOR_MODULE_RELPATH': 'contract', 'BuildGeneratorFn': 'contract', 'Generator': 'contract', 'NextInputFn': 'contract', 'GENERATOR_VLLM_MODULE_RELPATH': 'contract_vllm', 'ModelCapabilities': 'contract_vllm', 'VllmGeneratorAdapter': 'contract_vllm', 'generate_reference': 'generate', 'DEFAULT_K': 'generate', 'FORMAT_VERSION': 'schema', 'Reference': 'schema', 'ReferenceEntry': 'schema', 'load_reference': 'schema', 'save_reference': 'schema', 'TokenAccuracy': 'teacher_forcing'}
__all__ = list(_EXPORTS)


def __getattr__(name):
    if name not in _EXPORTS:
        raise AttributeError(name)
    value = getattr(import_module(f".{_EXPORTS[name]}", __name__), name)
    globals()[name] = value
    return value

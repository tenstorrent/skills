"""Serving CLI regressions, with all server/model/HTTP dependencies isolated."""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

PLUGIN = Path(__file__).resolve().parents[1] / 'plugins/tt-model-bringup'
RUNTIME = PLUGIN / 'runtime'


@pytest.fixture
def serving(monkeypatch):
    # These optional dependencies are never exercised by argv construction.
    # Stub only while loading an isolated module; do not contaminate other suites.
    monkeypatch.syspath_prepend(str(RUNTIME))
    with monkeypatch.context() as imports:
        for name in ('openai', 'requests', 'transformers'):
            stub = ModuleType(name)
            if name == 'transformers':
                stub.AutoTokenizer = Mock()
            imports.setitem(sys.modules, name, stub)
        spec = importlib.util.spec_from_file_location(
            'serving_cli_regression', RUNTIME / 'readiness_check/run_vllm_server.py'
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('mesh', ['N150', 'N300', 'P300x2', 'T3K', 'TG'])
def test_serving_cli_forwards_mesh_and_supported_config_flag(serving, monkeypatch, tmp_path, mesh):
    # Upstream vLLM owns --additional-config; the standalone plugin reads tt.
    # arg_utils.py registers --additional-config; TT config.py reads its tt object.
    config = {'sample_on_device_mode': 'all', 'cache_path': 'path with spaces', 'nested': {'enabled': True}}
    monkeypatch.setattr(sys, 'argv', [
        'run_vllm_server', '--model-dir', str(tmp_path), '--hf-model', 'org/model',
        '--mesh-device', mesh, '--stages', 'serve', '--tt-config', json.dumps(config),
        '--max-model-len', '8192', '--additional-server-args', '--served-model-name named-model',
    ])
    popen = Mock()
    monkeypatch.setattr(serving.subprocess, 'Popen', popen)
    for name in ('_check_port_available', '_wait_for_server', '_hold_until_signal', '_shutdown'):
        monkeypatch.setattr(serving, name, Mock())
    serving._main()
    cmd = popen.call_args.args[0]
    try:
        assert cmd[:3] == [sys.executable, '-m', 'vllm.entrypoints.openai.api_server']
        assert '--plugin-config' not in cmd
        assert cmd.count('--additional-config') == 1
        assert json.loads(cmd[cmd.index('--additional-config') + 1]) == {'tt': config}
        assert cmd[cmd.index('--max_model_len') + 1] == '8192'
        assert cmd[-2:] == ['--served-model-name', 'named-model']
        assert popen.call_args.kwargs['env']['MESH_DEVICE'] == mesh
        serving._wait_for_server.assert_called_once()
    finally:
        popen.call_args.kwargs['stdout'].close()


def test_unknown_mesh_is_rejected_before_launch(serving, monkeypatch, tmp_path):
    monkeypatch.setattr(sys, 'argv', [
        'run_vllm_server', '--model-dir', str(tmp_path), '--hf-model', 'org/model',
        '--mesh-device', 'invented-mesh', '--stages', 'serve',
    ])
    launch = Mock()
    monkeypatch.setattr(serving, '_launch_server', launch)
    with pytest.raises(SystemExit) as error:
        serving._main()
    assert error.value.code == 2
    launch.assert_not_called()


def test_qb2_shared_mesh_opens_four_devices_without_private_table_patch(serving, monkeypatch):
    from readiness_check import mesh_device
    assert serving.MESH_SHAPES is mesh_device.MESH_SHAPES
    parser = argparse.ArgumentParser()
    mesh_device.add_mesh_device_args(parser)
    args = parser.parse_args(['--mesh-device', 'P300x2'])
    ttnn = ModuleType('ttnn')
    ttnn.MeshShape = Mock(return_value='shape')
    ttnn.open_mesh_device = Mock(return_value='device')
    monkeypatch.setitem(sys.modules, 'ttnn', ttnn)
    assert mesh_device.open_readiness_mesh_device(args.mesh_device) == 'device'
    ttnn.MeshShape.assert_called_once_with(1, 4)
    ttnn.open_mesh_device.assert_called_once_with(mesh_shape='shape')


def install_fake_plugin(serving, monkeypatch, root):
    origin = root / 'src/vllm_tt_plugin/__init__.py'
    origin.parent.mkdir(parents=True)
    origin.touch()
    tests = root / 'tests/tt'
    tests.mkdir(parents=True)
    (tests / 'conftest.py').touch()
    monkeypatch.delenv('VLLM_TT_PLUGIN_ROOT', raising=False)
    monkeypatch.setattr(serving.importlib.util, 'find_spec',
                        lambda name: SimpleNamespace(origin=str(origin)) if name == 'vllm_tt_plugin' else None)
    return tests


@pytest.mark.parametrize('explicit', [False, True])
def test_sampling_uses_installed_standalone_checkout(serving, monkeypatch, tmp_path, explicit):
    root = tmp_path / 'standalone'
    tests = install_fake_plugin(serving, monkeypatch, root)
    if explicit:
        monkeypatch.setenv('VLLM_TT_PLUGIN_ROOT', str(root))
    assert serving._find_plugin_tests_dir() == tests
    run = Mock(return_value=SimpleNamespace(returncode=0))
    monkeypatch.setattr(serving.subprocess, 'run', run)
    assert serving._run_plugin_sampling_tests(
        server_url='http://localhost:8000', hf_model='org/model', log_file=tmp_path/'tests.log',
        server_log=tmp_path/'server.log', max_num_seqs=8, sampling_profile='full')
    assert run.call_args.args[0][3] == str(tests)
    assert '--tt-max-num-seqs=8' in run.call_args.args[0]


def test_sampling_rejects_other_checkout_tests(serving, monkeypatch, tmp_path):
    install_fake_plugin(serving, monkeypatch, tmp_path/'installed')
    monkeypatch.setenv('VLLM_TT_PLUGIN_ROOT', str(tmp_path/'different'))
    with pytest.raises(RuntimeError, match='does not match installed'):
        serving._find_plugin_tests_dir()


def test_sampling_rejects_legacy_bundled_plugin(serving, monkeypatch, tmp_path):
    (tmp_path/'fork/vllm').mkdir(parents=True)
    install_fake_plugin(serving, monkeypatch, tmp_path/'fork/plugins/vllm-tt-plugin')
    with pytest.raises(RuntimeError, match='Legacy bundled'):
        serving._find_plugin_tests_dir()


def test_sampling_does_not_fall_back_to_core_tests(serving, monkeypatch, tmp_path):
    tests = install_fake_plugin(serving, monkeypatch, tmp_path/'standalone')
    (tests/'conftest.py').unlink()
    core = tmp_path/'core/vllm/__init__.py'
    core.parent.mkdir(parents=True)
    core.touch()
    (tmp_path/'core/tests/tt').mkdir(parents=True)
    plugin_origin = tmp_path/'standalone/src/vllm_tt_plugin/__init__.py'
    monkeypatch.setattr(serving.importlib.util, 'find_spec', lambda name:
                        SimpleNamespace(origin=str(plugin_origin if name == 'vllm_tt_plugin' else core)))
    with pytest.raises(RuntimeError, match='Canonical standalone plugin tests missing'):
        serving._find_plugin_tests_dir()


def test_sampling_requires_installed_plugin(serving, monkeypatch):
    monkeypatch.setattr(serving.importlib.util, 'find_spec', lambda name: None)
    with pytest.raises(RuntimeError, match='Install the standalone'):
        serving._find_plugin_tests_dir()


def test_missing_sampling_suite_fails_before_server_launch(serving, monkeypatch, tmp_path):
    monkeypatch.setattr(sys, 'argv', ['run_vllm_server', '--model-dir', str(tmp_path),
                                   '--hf-model', 'org/model', '--mesh-device', 'N150'])
    monkeypatch.setattr(serving.importlib.util, 'find_spec', lambda name: None)
    launch = Mock()
    monkeypatch.setattr(serving, '_launch_server', launch)
    with pytest.raises(RuntimeError, match='Install the standalone'):
        serving._main()
    launch.assert_not_called()

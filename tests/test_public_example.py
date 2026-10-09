"""公开示例必须在网络和模型导入均被拒绝时完成闭环。"""
import builtins
from pathlib import Path
import runpy
import socket


def test_minimal_example_is_offline_without_models(monkeypatch):
    real_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name.split('.')[0] in {'torch', 'transformers', 'huggingface_hub'}:
            raise AssertionError(f'Model dependency imported: {name}')
        return real_import(name, *args, **kwargs)

    def forbidden(*args, **kwargs):
        raise AssertionError('Minimal example attempted network access')

    monkeypatch.setattr(builtins, '__import__', guarded_import)
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)
    monkeypatch.setattr(socket, 'getaddrinfo', forbidden)
    from tuomin_gateway.licensing import configure_enforcement
    configure_enforcement(None)
    monkeypatch.delenv('TUOMIN_LICENSE_REQUIRED', raising=False)
    module = runpy.run_path(str(Path(__file__).parents[1] / 'examples/minimal_demo.py'))
    result = module['run_demo']()
    assert result['restored'] == result['original']
    assert result['redacted'] != result['original']
    assert result['invalid_refill'] == 'blocked'

"""旧 env 转译为池；preferred 不得推翻本地优先和公平调度。"""
import os
import sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_original = Path.exists
with patch.object(Path, 'exists', lambda p: False if p.name == '.env' else _original(p)):
    from app.engines import build_registry, reset_registry, EnginePoolFacade


def main():
    with patch.dict(os.environ, {}, clear=True):
        pool = build_registry(force=True).engines[0]
        assert isinstance(pool, EnginePoolFacade)
        assert [s.provider for s, _ in pool.resources] == ['local']
    with patch.dict(os.environ, {'AUTODL_API_TOKEN': 'fake', 'TTS_ENGINE_PREFERRED': 'indextts_art'}, clear=True):
        pool = build_registry(force=True).engines[0]
        assert [s.tier for s, _ in pool.resources] == ['local', 'cloud']
        assert [s.provider for s, _ in pool.resources] == ['local', 'art']
    reset_registry()
    print('旧 env 资源池语义：通过')


if __name__ == '__main__':
    main()

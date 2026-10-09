"""NER model source resolution: packaged builds pin a local directory."""
from __future__ import annotations

from tuomin_gateway.detectors.ner import MODEL_NAME, MODEL_REVISION, _model_source


def test_default_uses_pinned_hf_id_with_revision(monkeypatch):
    monkeypatch.delenv("TUOMIN_NER_MODEL_DIR", raising=False)
    source, kwargs = _model_source(MODEL_NAME, MODEL_REVISION)
    assert source == MODEL_NAME
    assert kwargs == {"revision": MODEL_REVISION}


def test_env_dir_overrides_and_drops_revision(monkeypatch):
    monkeypatch.setenv("TUOMIN_NER_MODEL_DIR", "/opt/tuomin/models/cluener")
    source, kwargs = _model_source(MODEL_NAME, MODEL_REVISION)
    assert source == "/opt/tuomin/models/cluener"
    assert kwargs == {}  # local path: no revision, no HF lookup

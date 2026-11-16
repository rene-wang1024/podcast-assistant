import json
import shutil
from pathlib import Path

import pytest

from pipeline import edl
from pipeline.config import ROOT, load_config
from pipeline.runner import run_workflow
from pipeline.spec import load_spec, render


@pytest.fixture
def episodes(tmp_path, monkeypatch):
    monkeypatch.setenv("PIPELINE_PROVIDER_OVERRIDE", "mock")
    shutil.copytree(ROOT / "examples" / "EP00", tmp_path / "EP00")
    return tmp_path


def test_config_routes_each_workflow():
    cfg = load_config()
    assert [w.id for w in cfg.workflows] == ["rough_cut", "fine_cut", "shownotes", "cover", "distribution"]
    assert cfg.get("rough_cut").executor.model.endswith("codex")
    assert cfg.get("cover").executor.kind == "image"
    for wf in cfg.workflows:
        assert wf.executor.label() != wf.reviewer.label(), f"{wf.id} 执行和审查不应是同一个模型"


def test_env_override(monkeypatch):
    monkeypatch.setenv("PIPELINE_MODEL_COVER", "gpt-image-x")
    assert load_config().get("cover").executor.model == "gpt-image-x"


def test_specs_have_required_sections():
    for wf in load_config().workflows:
        spec = load_spec(wf.spec)
        assert spec.delivery_standard, wf.id
        assert spec.prompt_template, wf.id
        assert "{{" in spec.prompt_template


def test_render_fills_missing_with_placeholder():
    assert render("a{{x}}b{{y}}", {"x": "1"}) == "a1b（无）"


def test_full_run_with_mock(episodes):
    cfg = load_config()
    for wf in cfg.workflows:
        res = run_workflow(cfg, wf.id, "EP00", episodes_dir=episodes)
        assert res.output_path.exists()
        assert (res.output_path.parent / "review.md").exists()
    assert (episodes / "EP00" / "fine_cut" / "upstream_review.md").exists()
    run = json.loads((episodes / "EP00" / "cover" / "run.json").read_text(encoding="utf-8"))
    assert run["executor"].startswith("mock:")


def test_edl_kept_segments_and_validation():
    data = {"source_duration": 60.0, "cuts": [
        {"id": "A1", "layer": "A", "start": 0.0, "end": 6.2, "text": "开始了"},
        {"id": "C1", "layer": "C", "start": 19.5, "end": 20.3, "text": "就是"},
        {"id": "B1", "layer": "B", "start": 33.0, "end": 41.0, "text": "推测"},
    ]}
    assert edl.validate(data) == []
    assert edl.kept_segments(data) == [(6.2, 19.5), (20.3, 33.0), (41.0, 60.0)]
    assert edl.summarize(data)["kept_ratio"] == round((60 - 6.2 - 0.8 - 8) / 60, 3)
    bad = {"source_duration": 10.0, "cuts": [
        {"id": "X", "layer": "Z", "start": 5, "end": 4, "text": ""},
        {"id": "Y", "layer": "C", "start": 3, "end": 12, "text": "a"},
    ]}
    assert len(edl.validate(bad)) >= 4

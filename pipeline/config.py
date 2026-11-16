"""读取流水线配置，支持用环境变量覆盖每个工作流的模型。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class ModelRef:
    provider: str
    model: str = "default"
    kind: str = "text"  # text | image

    def label(self) -> str:
        return f"{self.provider}:{self.model}"


@dataclass
class Workflow:
    id: str
    name: str
    spec: Path
    executor: ModelRef
    reviewer: ModelRef
    input: str
    output: str


@dataclass
class Config:
    show: dict
    workflows: list[Workflow]
    review: dict = field(default_factory=dict)

    def get(self, wf_id: str) -> Workflow:
        for wf in self.workflows:
            if wf.id == wf_id:
                return wf
        ids = ", ".join(w.id for w in self.workflows)
        raise KeyError(f"未知工作流 {wf_id!r}，可选：{ids}")

    def upstream_of(self, wf_id: str) -> Workflow | None:
        ids = [w.id for w in self.workflows]
        i = ids.index(wf_id)
        return self.workflows[i - 1] if i > 0 else None


def _model(d: dict, env_key: str) -> ModelRef:
    ref = ModelRef(provider=d["provider"], model=str(d.get("model", "default")), kind=d.get("kind", "text"))
    # 环境变量覆盖：PIPELINE_MODEL_<WF> / PIPELINE_PROVIDER_<WF>
    ref.model = os.environ.get(f"PIPELINE_MODEL_{env_key}", ref.model)
    ref.provider = os.environ.get(f"PIPELINE_PROVIDER_{env_key}", ref.provider)
    # 全局覆盖，用于试跑和测试：PIPELINE_PROVIDER_OVERRIDE=mock
    ref.provider = os.environ.get("PIPELINE_PROVIDER_OVERRIDE", ref.provider)
    return ref


def load_show(path: Path | None = None) -> dict:
    path = path or Path(os.environ.get("PIPELINE_SHOW", ROOT / "config" / "show.yaml"))
    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}


def load_config(path: Path | None = None, show_path: Path | None = None) -> Config:
    path = path or ROOT / "config" / "pipeline.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    wfs = []
    for w in data["workflows"]:
        key = w["id"].upper()
        wfs.append(
            Workflow(
                id=w["id"],
                name=w["name"],
                spec=ROOT / w["spec"],
                executor=_model(w["executor"], key),
                reviewer=_model(w["reviewer"], f"{key}_REVIEWER"),
                input=w["input"],
                output=w["output"],
            )
        )
    return Config(show=load_show(show_path), workflows=wfs, review=data.get("review", {}))

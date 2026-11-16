"""命令行模式：按期号目录读写文件，运行单个工作流。

目录约定（每期一个文件夹）：
    episodes/<期号>/
        transcript.json           # 原始输入：逐字转写
        final_transcript.txt      # 精剪定稿后的文字稿
        constraints.md            # 可选：本期硬约束（标红原话、制片要求）
        rough_cut/edl.json        # 各工作流的产出
        rough_cut/review.md       # 本工作流的独立审查
        fine_cut/upstream_review.md  # 精剪开工前对粗剪的审查
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import edl as edl_mod
from .config import ROOT, Config
from .core import build_prompt, extract_json, review, review_markdown
from .providers import get_provider
from .spec import load_spec


class ReviewFailed(RuntimeError):
    pass


@dataclass
class RunResult:
    workflow: str
    output_path: Path
    review: dict
    upstream_review: dict | None


def _find_input(ep_dir: Path, filename: str) -> Path:
    for p in [ep_dir / filename, *sorted(ep_dir.glob(f"*/{filename}"))]:
        if p.exists():
            return p
    raise FileNotFoundError(f"在 {ep_dir} 里找不到输入文件 {filename}")


def _read(path: Path, limit: int = 400_000) -> str:
    if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
        return f"（图片文件：{path.name}，{path.stat().st_size} 字节）"
    text = path.read_text(encoding="utf-8")
    return text if len(text) <= limit else text[:limit] + "\n…（已截断）"


def run_workflow(cfg: Config, wf_id: str, episode: str, *, force: bool = False, extra_vars: dict | None = None,
                 episodes_dir: Path | None = None) -> RunResult:
    wf = cfg.get(wf_id)
    spec = load_spec(wf.spec)
    ep_dir = (episodes_dir or ROOT / "episodes") / episode
    out_dir = ep_dir / wf.id
    out_dir.mkdir(parents=True, exist_ok=True)
    input_path = _find_input(ep_dir, wf.input)
    input_text = _read(input_path)

    # 1. 下游审上游：用本工作流的执行模型，按上游的交付标准审上游产出
    upstream_review = None
    up = cfg.upstream_of(wf.id)
    if up and cfg.review.get("downstream_reviews_upstream", True):
        up_out = ep_dir / up.id / up.output
        if up_out.exists():
            up_spec = load_spec(up.spec)
            upstream_review = review(up, _read(up_out), wf.executor,
                                     images=[up_out] if up.executor.kind == "image" else None,
                                     extra=f"\n【下游开工前重点检查】\n{up_spec.upstream_checks}\n{spec.upstream_checks}\n")
            (out_dir / "upstream_review.md").write_text(review_markdown(f"{wf.name}对{up.name}的审查", upstream_review), encoding="utf-8")
            if upstream_review["verdict"] != "pass" and cfg.review.get("block_on_fail", True) and not force:
                raise ReviewFailed(f"{wf.name}开工前审查{up.name}未通过，见 {out_dir / 'upstream_review.md'}；确认后可加 --force 继续")

    # 2. 执行
    constraints = ep_dir / "constraints.md"
    prompt = build_prompt(wf, cfg.show, input_text, upstream_review=upstream_review, extra=extra_vars,
                          constraints=constraints.read_text(encoding="utf-8") if constraints.exists() else "")
    provider = get_provider(wf.executor)
    out_path = out_dir / wf.output
    if wf.executor.kind == "image":
        (out_dir / "cover_prompt.txt").write_text(prompt, encoding="utf-8")
        provider.image(prompt, out_path)
        deliverable = f"图像 prompt：\n{prompt}\n（图片已生成：{out_path.name}）"
    else:
        raw = provider.complete(prompt, workdir=ep_dir)
        if wf.output.endswith(".json"):
            data = extract_json(raw)
            if isinstance(data, dict) and "cuts" in data:
                data.setdefault("episode", episode)
                problems = edl_mod.validate(data) if data.get("source_duration") else []
                if problems:
                    data["validation_problems"] = problems
            out_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            out_path.write_text(raw, encoding="utf-8")
        deliverable = _read(out_path)

    # 3. 本工作流的独立审查（换模型）
    result = review(wf, deliverable, wf.reviewer, images=[out_path] if wf.executor.kind == "image" else None)
    (out_dir / "review.md").write_text(review_markdown(f"{wf.name}交付审查", result), encoding="utf-8")
    (out_dir / "run.json").write_text(json.dumps({
        "workflow": wf.id, "episode": episode, "time": datetime.now().isoformat(timespec="seconds"),
        "executor": wf.executor.label(), "reviewer": wf.reviewer.label(),
        "input": str(input_path.relative_to(ep_dir)), "output": wf.output, "verdict": result.get("verdict"),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    return RunResult(wf.id, out_path, result, upstream_review)

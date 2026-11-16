"""命令行入口。

    python -m pipeline plan                         # 看每个工作流用哪个模型
    python -m pipeline run rough_cut --episode EP37 # 跑单个工作流
    python -m pipeline run all --episode EP37       # 依次跑全部（遇到审查不通过会停）
    python -m pipeline check-edl episodes/EP37/rough_cut/edl.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import edl as edl_mod
from .config import load_config
from .runner import ReviewFailed, run_workflow


def cmd_plan(cfg, _args):
    print(f"《{cfg.show.get("name", "")}》制作流水线\n")
    for i, wf in enumerate(cfg.workflows, 1):
        print(f"{i}. {wf.name}  [{wf.id}]")
        print(f"   执行：{wf.executor.label()}    审查：{wf.reviewer.label()}")
        print(f"   {wf.input} → {wf.output}")
    if cfg.review.get("downstream_reviews_upstream", True):
        print("\n每个工作流开工前，由它的执行模型按上游交付标准审一遍上游产出。")


def cmd_run(cfg, args):
    ids = [w.id for w in cfg.workflows] if args.workflow == "all" else [args.workflow]
    extra = dict(kv.split("=", 1) for kv in args.var or [])
    for wf_id in ids:
        wf = cfg.get(wf_id)
        print(f"▶ {wf.name}（{wf.executor.label()}）…", flush=True)
        try:
            res = run_workflow(cfg, wf_id, args.episode, force=args.force, extra_vars=extra)
        except ReviewFailed as e:
            print(f"✗ {e}")
            return 2
        except FileNotFoundError as e:
            print(f"✗ {e}")
            return 1
        mark = "✓" if res.review.get("verdict") == "pass" else "!"
        print(f"  {mark} 产出 {res.output_path}；审查（{res.review.get('reviewer')}）：{res.review.get('verdict')} — {res.review.get('summary', '')}")
    return 0


def cmd_check_edl(_cfg, args):
    data = json.loads(Path(args.path).read_text(encoding="utf-8"))
    problems = edl_mod.validate(data, args.duration)
    for p in problems:
        print("✗", p)
    if data.get("source_duration") or args.duration:
        print(json.dumps(edl_mod.summarize(data, args.duration), ensure_ascii=False, indent=2))
    return 1 if problems else 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="pipeline", description="播客制作流水线：不同工作流调用不同模型")
    p.add_argument("--config", type=Path, help="配置文件路径，默认 config/pipeline.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("plan", help="列出各工作流的模型分配")
    r = sub.add_parser("run", help="运行工作流")
    r.add_argument("workflow", help="工作流 id，或 all")
    r.add_argument("--episode", required=True, help="期号，对应 episodes/<期号>/")
    r.add_argument("--force", action="store_true", help="上游审查不通过时也继续")
    r.add_argument("--var", action="append", metavar="KEY=VALUE", help="额外的 prompt 变量，如 --var target=video")
    c = sub.add_parser("check-edl", help="校验 EDL 并统计保留时长")
    c.add_argument("path")
    c.add_argument("--duration", type=float, help="素材总时长（秒），EDL 里没有 source_duration 时需要")
    args = p.parse_args(argv)
    cfg = load_config(args.config)
    return {"plan": cmd_plan, "run": cmd_run, "check-edl": cmd_check_edl}[args.cmd](cfg, args) or 0


if __name__ == "__main__":
    sys.exit(main())

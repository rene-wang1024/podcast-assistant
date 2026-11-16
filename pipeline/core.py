"""与存储无关的核心逻辑：组装 prompt、调用执行模型、独立审查。

命令行（runner.py）和网页工作台（app/）都调用这里。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .config import ROOT, ModelRef, Workflow
from .providers import get_provider
from .spec import load_spec, render

REVIEW_TEMPLATE = """你是独立审查员，和产出这份交付物的模型不是同一个。请只依据下面的交付标准逐条审查，不要改写交付物。

【工作流】{name}
【交付标准】
{standard}
{extra}
【交付物】
{deliverable}

只输出 JSON：{{"verdict": "pass" 或 "fail", "items": [{{"standard": "标准原文", "ok": true/false, "note": "依据或问题"}}], "summary": "一句话结论"}}"""


def extract_json(text: str):
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    raw = m.group(1) if m else text
    starts = [i for i in (raw.find("{"), raw.find("[")) if i >= 0]
    obj, _ = json.JSONDecoder().raw_decode(raw[min(starts) if starts else 0:])  # 忽略 JSON 之后的多余文字
    return obj


def _bullets(v) -> str:
    if isinstance(v, (list, tuple)):
        return "\n".join(f"- {x}" for x in v)
    return str(v or "")


def show_variables(show: dict) -> dict[str, str]:
    brand = show.get("brand") or {}
    return {
        "show_name": show.get("name", ""),
        "show_tagline": show.get("tagline", ""),
        "show_domain": show.get("domain", ""),
        "show_format": show.get("format", ""),
        "accuracy_rules": _bullets(show.get("accuracy_rules")),
        "disclaimer": show.get("disclaimer", ""),
        "platforms": _bullets(show.get("platforms")),
        "brand_style": "\n".join(f"{k}: {v}" for k, v in brand.items()) if isinstance(brand, dict) else str(brand),
    }


def cover_variables(shownotes: str) -> dict[str, str]:
    def section(keyword: str) -> str:
        m = re.search(rf"^#+[^\n]*{keyword}[^\n]*\n(.*?)(?=^#|\Z)", shownotes, re.M | re.S)
        return m.group(1).strip() if m else ""

    titles = re.findall(r"^\s*(?:[-*]|\d+\.)\s*(.+)$", section("标题"), re.M)
    first = shownotes.strip().splitlines()[0].lstrip("# ") if shownotes.strip() else ""
    return {"title": titles[0] if titles else first, "keywords": section("关键词").replace("\n", " ")}


def build_prompt(wf: Workflow, show: dict, input_text: str, *, upstream_review: dict | None = None,
                 constraints: str = "", extra: dict | None = None) -> str:
    spec = load_spec(wf.spec)
    variables = {
        **show_variables(show),
        "editing_rules": (ROOT / "docs" / "editing-rules.md").read_text(encoding="utf-8"),
        "constraints": constraints,
        "delivery_standard": spec.delivery_standard,
        "upstream_review": json.dumps(upstream_review, ensure_ascii=False, indent=1) if upstream_review else "",
        "input": input_text,
        "target": "audio",
    }
    if wf.executor.kind == "image":
        variables.update(cover_variables(input_text))
    variables.update(extra or {})
    return render(spec.prompt_template, variables)


def review(wf: Workflow, deliverable: str, reviewer: ModelRef, *, extra: str = "",
           images: list[Path] | None = None) -> dict:
    spec = load_spec(wf.spec)
    prompt = REVIEW_TEMPLATE.format(name=wf.name, standard=spec.delivery_standard, extra=extra, deliverable=deliverable)
    raw = get_provider(reviewer).complete(prompt, images=images)
    try:
        result = extract_json(raw)
        if not isinstance(result, dict):
            raise ValueError
    except (json.JSONDecodeError, ValueError):
        result = {"verdict": "fail", "items": [], "summary": "审查结果无法解析", "raw": raw}
    result["reviewer"] = reviewer.label()
    return result


def generate_text(wf: Workflow, show: dict, input_text: str, **kw) -> tuple[str, dict]:
    """执行一个文字类工作流并做独立审查，返回 (产出, 审查结果)。"""
    prompt = build_prompt(wf, show, input_text, **kw)
    output = get_provider(wf.executor).complete(prompt)
    return output, review(wf, output, wf.reviewer)


def review_markdown(title: str, r: dict) -> str:
    lines = [f"# {title}", "", f"- 审查模型：`{r.get('reviewer')}`", f"- 结论：**{r.get('verdict')}**", f"- {r.get('summary', '')}", ""]
    for it in r.get("items", []):
        lines.append(f"- [{'x' if it.get('ok') else ' '}] {it.get('standard')} —— {it.get('note', '')}")
    return "\n".join(lines) + "\n"

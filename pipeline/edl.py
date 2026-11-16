"""EDL 校验与保留区间计算。格式见 docs/edl-schema.md。"""
from __future__ import annotations

VALID_LAYERS = {"A", "B", "B'", "C"}


def validate(edl: dict, source_duration: float | None = None) -> list[str]:
    """返回问题列表；空列表表示通过。"""
    problems: list[str] = []
    dur = source_duration or edl.get("source_duration")
    cuts = sorted(edl.get("cuts", []), key=lambda c: c["start"])
    seen: set[str] = set()
    for c in cuts:
        cid = c.get("id", "?")
        if cid in seen:
            problems.append(f"{cid}: id 重复")
        seen.add(cid)
        if c.get("layer") not in VALID_LAYERS:
            problems.append(f"{cid}: layer 应为 A/B/B'/C")
        if c["end"] <= c["start"]:
            problems.append(f"{cid}: 结束时间不晚于开始时间")
        if c["start"] < 0 or (dur and c["end"] > dur + 1e-6):
            problems.append(f"{cid}: 超出素材范围")
        if not c.get("text"):
            problems.append(f"{cid}: 缺少被删原文 text")
    for a, b in zip(cuts, cuts[1:]):
        if b["start"] < a["end"] - 1e-6:
            problems.append(f"{a.get('id')} 与 {b.get('id')} 重叠")
    return problems


def merge(*edls: dict) -> dict:
    """合并粗剪和精剪的剪点。"""
    base = dict(edls[0])
    base["cuts"] = [c for e in edls for c in e.get("cuts", [])]
    return base


def kept_segments(edl: dict, source_duration: float | None = None) -> list[tuple[float, float]]:
    """由剪点算出保留区间（原始时间线），重叠的剪点会先合并。"""
    dur = source_duration or edl["source_duration"]
    spans = sorted((c["start"], c["end"]) for c in edl.get("cuts", []))
    merged: list[list[float]] = []
    for s, e in spans:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    kept, cursor = [], 0.0
    for s, e in merged:
        if s > cursor:
            kept.append((round(cursor, 3), round(s, 3)))
        cursor = max(cursor, e)
    if cursor < dur:
        kept.append((round(cursor, 3), round(dur, 3)))
    return kept


def summarize(edl: dict, source_duration: float | None = None) -> dict:
    dur = source_duration or edl["source_duration"]
    kept = sum(e - s for s, e in kept_segments(edl, dur))
    by_layer: dict[str, dict] = {}
    for c in edl.get("cuts", []):
        d = by_layer.setdefault(c["layer"], {"count": 0, "seconds": 0.0})
        d["count"] += 1
        d["seconds"] = round(d["seconds"] + c["end"] - c["start"], 2)
    return {"kept_duration": round(kept, 2), "kept_ratio": round(kept / dur, 3), "by_layer": by_layer}

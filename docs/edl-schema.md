# EDL（剪点方案）格式

粗剪和精剪都输出同一种 JSON。时间单位为秒，以原始素材时间线为准。

```json
{
  "episode": "EP00",
  "source_duration": 4800.0,
  "cuts": [
    {
      "id": "A001",
      "layer": "A",
      "start": 0.0,
      "end": 42.3,
      "text": "（开场前调试）",
      "before": "",
      "after": "欢迎收听本期节目",
      "reason": "录制废料：开场前调试",
      "needs_confirm": false
    },
    {
      "id": "C014",
      "layer": "C",
      "start": 312.84,
      "end": 313.21,
      "text": "就是",
      "before": "这个罪名",
      "after": "它的核心是明知",
      "reason": "句首口水词",
      "needs_confirm": false
    }
  ],
  "summary": {
    "kept_duration": 3120.0,
    "kept_ratio": 0.65,
    "by_layer": { "A": {"count": 3, "seconds": 380.0}, "B": {"count": 9, "seconds": 600.0}, "C": {"count": 412, "seconds": 700.0} }
  }
}
```

| 字段 | 说明 |
|---|---|
| `layer` | `A` 录制废料 / `B` 整段 / `B'` 分句短段 / `C` 句内逐字 |
| `text` | 被删除的原文，便于人工核对 |
| `before` / `after` | 剪点前后各几个保留字，用于检查接口是否通顺 |
| `needs_confirm` | 涉及观点取舍、法律内容或语义不明时为 `true` |

`pipeline/edl.py` 提供校验（剪点不重叠、在素材范围内）和由 EDL 计算保留区间 `kept_segments` 的工具函数，保留区间可以交给你自己的剪辑软件写回脚本。

"""企业微信群机器人通知。

在企业微信群里：群设置 → 群机器人 → 添加，复制 Webhook 地址，填到工作台「设置」页。
@ 人用的是成员的手机号（成员页里填写），需要该成员在企业微信里绑定了这个手机号。
"""
from __future__ import annotations

import json
import urllib.request


def send_wecom(webhook: str, content: str, mobiles: list[str] | None = None, timeout: float = 5.0) -> tuple[bool, str]:
    if not webhook:
        return False, "未配置企业微信 Webhook"
    payload = {"msgtype": "text", "text": {"content": content, "mentioned_mobile_list": [m for m in mobiles or [] if m]}}
    req = urllib.request.Request(webhook, data=json.dumps(payload, ensure_ascii=False).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode() or "{}")
        ok = body.get("errcode", 0) == 0
        return ok, "已发送" if ok else f"企业微信返回错误：{body}"
    except Exception as e:  # 通知失败不影响流转
        return False, f"发送失败：{e}"

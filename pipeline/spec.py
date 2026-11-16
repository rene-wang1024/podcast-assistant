"""解析 workflows/*.md：岗位职责、交付标准、审上游要点和 prompt 模板。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Spec:
    title: str
    sections: dict[str, str]

    @property
    def delivery_standard(self) -> str:
        return self.sections.get("交付标准", "")

    @property
    def upstream_checks(self) -> str:
        for k, v in self.sections.items():
            if k.startswith("审上游"):
                return v
        return ""

    @property
    def prompt_template(self) -> str:
        body = self.sections.get("Prompt", "")
        m = re.search(r"```[a-z]*\n(.*?)```", body, re.S)
        return (m.group(1) if m else body).strip()


def load_spec(path: Path) -> Spec:
    text = path.read_text(encoding="utf-8")
    title_m = re.search(r"^# (.+)$", text, re.M)
    sections: dict[str, str] = {}
    current = None
    buf: list[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            if current:
                sections[current] = "\n".join(buf).strip()
            current, buf = line[3:].strip(), []
        elif current:
            buf.append(line)
    if current:
        sections[current] = "\n".join(buf).strip()
    return Spec(title=title_m.group(1) if title_m else path.stem, sections=sections)


def render(template: str, variables: dict[str, str]) -> str:
    """把 {{name}} 替换成变量；没提供的变量替换为「（无）」。"""
    return re.sub(r"\{\{(\w+)\}\}", lambda m: str(variables.get(m.group(1)) or "（无）"), template)

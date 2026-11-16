"""模型提供方。每个工作流按配置路由到不同的提供方。

新增提供方：写一个 Provider 子类，实现 complete()（以及需要时的 image()），
再在 PROVIDERS 里登记名字即可。
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .config import ModelRef


def _mime(path: Path) -> str:
    return {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}.get(path.suffix.lower(), "image/png")


def _data_url(path: Path) -> str:
    return f"data:{_mime(path)};base64," + base64.b64encode(path.read_bytes()).decode()


class Provider:
    def __init__(self, ref: ModelRef):
        self.ref = ref

    def complete(self, prompt: str, *, workdir: Path | None = None, images: list[Path] | None = None) -> str:
        raise NotImplementedError

    def image(self, prompt: str, out_path: Path, size: str = "1024x1024") -> Path:
        raise NotImplementedError(f"{self.ref.provider} 不支持生成图片")


class MockProvider(Provider):
    """不调用模型。文字返回可解析的占位结果，图片写一张 1×1 PNG。"""

    PNG_1x1 = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8/x8AAwMCAO+ip1sAAAAASUVORK5CYII="
    )

    def complete(self, prompt: str, *, workdir: Path | None = None, images: list[Path] | None = None) -> str:
        if "独立审查员" in prompt:
            return json.dumps({"verdict": "pass", "items": [], "summary": "mock 审查：未实际检查"}, ensure_ascii=False)
        if "JSON" in prompt:
            return json.dumps({"cuts": [], "summary": {"note": "mock 输出"}}, ensure_ascii=False)
        return "# mock 输出\n\n" + prompt[:500]

    def image(self, prompt: str, out_path: Path, size: str = "1024x1024") -> Path:
        out_path.write_bytes(self.PNG_1x1)
        return out_path


class OpenAIProvider(Provider):
    def _client(self):
        from openai import OpenAI  # 延迟导入：不用 OpenAI 的工作流不需要装

        return OpenAI()

    def complete(self, prompt: str, *, workdir: Path | None = None, images: list[Path] | None = None) -> str:
        content = [{"type": "input_text", "text": prompt}]
        for img in images or []:
            content.append({"type": "input_image", "image_url": _data_url(img)})
        resp = self._client().responses.create(model=self.ref.model, input=[{"role": "user", "content": content}])
        return resp.output_text

    def image(self, prompt: str, out_path: Path, size: str = "1024x1024") -> Path:
        resp = self._client().images.generate(model=self.ref.model, prompt=prompt, size=size)
        out_path.write_bytes(base64.b64decode(resp.data[0].b64_json))
        return out_path


class AnthropicProvider(Provider):
    def complete(self, prompt: str, *, workdir: Path | None = None, images: list[Path] | None = None) -> str:
        import anthropic

        client = anthropic.Anthropic()
        msg = client.messages.create(
            model=self.ref.model,
            max_tokens=int(os.environ.get("PIPELINE_MAX_TOKENS", "16000")),
            messages=[{"role": "user", "content": [
                *({"type": "image", "source": {"type": "base64", "media_type": _mime(img),
                   "data": base64.b64encode(img.read_bytes()).decode()}} for img in images or []),
                {"type": "text", "text": prompt},
            ]}],
        )
        return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")


class CodexProvider(Provider):
    """调用本地 Codex CLI（codex exec）。适合需要读写本地工程文件的工作流。

    Codex 在 workdir 里运行，可以读取同目录的转写、工程文件；
    最终回答通过 -o 写到临时文件再读回来。
    """

    def complete(self, prompt: str, *, workdir: Path | None = None, images: list[Path] | None = None) -> str:
        if not shutil.which("codex"):
            raise RuntimeError("找不到 codex 命令。请先安装 Codex CLI，或在 config/pipeline.yaml 里换成别的提供方。")
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "last_message.txt"
            cmd = ["codex", "exec", "--skip-git-repo-check", "--sandbox", os.environ.get("PIPELINE_CODEX_SANDBOX", "read-only"), "-o", str(out)]
            if workdir:
                cmd += ["-C", str(workdir)]
            for img in images or []:
                cmd += ["-i", str(img)]
            if self.ref.model not in ("", "default"):
                cmd += ["-m", self.ref.model]
            cmd.append("-")  # 从 stdin 读 prompt，避免命令行长度限制
            subprocess.run(cmd, input=prompt, text=True, check=True)
            return out.read_text(encoding="utf-8")


PROVIDERS: dict[str, type[Provider]] = {
    "mock": MockProvider,
    "openai": OpenAIProvider,
    "anthropic": AnthropicProvider,
    "codex": CodexProvider,
}


def get_provider(ref: ModelRef) -> Provider:
    try:
        return PROVIDERS[ref.provider](ref)
    except KeyError:
        raise ValueError(f"未知提供方 {ref.provider!r}，可选：{', '.join(PROVIDERS)}") from None

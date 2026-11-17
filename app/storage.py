"""文件存储。

大文件（原片、粗剪视频、成品）用分片上传：浏览器把文件切成固定大小的分片，
逐片上传，断网或关掉页面后，重新选同一个文件会从没传完的分片继续。

现在只有本地磁盘实现（LocalStorage）。接口按对象存储的分片上传设计
（初始化 → 每片拿上传地址 → 合并），以后接阿里云 OSS 时，
浏览器直接把分片传到 OSS，只需新增一个实现，网页和数据库不用改。
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import AsyncIterator

PART_SIZE = int(os.environ.get("UPLOAD_PART_BYTES") or int(os.environ.get("UPLOAD_PART_MB", "8")) * 1024 * 1024)


class StorageError(Exception):
    pass


class LocalStorage:
    """文件放在 root 下，key 是相对路径。

    分片直接写进 <key>.partial 的对应位置（不另存分片再拼接），
    所以 50 GB 的原片只占 50 GB 磁盘。全部分片到齐后改名为正式文件。
    """

    name = "local"

    def __init__(self, root: Path):
        self.root = Path(root)

    def path(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if self.root.resolve() not in p.parents:
            raise StorageError("非法路径")
        return p

    def _partial(self, key: str) -> Path:
        return self.path(key).with_name(self.path(key).name + ".partial")

    # ---- 分片上传 ----
    def init_multipart(self, key: str, size: int) -> None:
        p = self._partial(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("ab") as f:  # 已存在则保留（续传）
            f.truncate(size)

    def part_target(self, upload_id: str, n: int) -> dict:
        """浏览器上传第 n 片（从 0 开始）用的地址。OSS 实现会返回预签名 URL。"""
        return {"method": "PUT", "url": f"/api/uploads/{upload_id}/parts/{n}", "headers": {}}

    async def write_part(self, key: str, offset: int, expected: int, stream: AsyncIterator[bytes]) -> str:
        p = self._partial(key)
        if not p.exists():
            raise StorageError("上传不存在或已结束")
        md5 = hashlib.md5()
        got = 0
        with p.open("r+b") as f:
            f.seek(offset)
            async for chunk in stream:
                got += len(chunk)
                if got > expected:
                    raise StorageError("分片大小不对")
                md5.update(chunk)
                f.write(chunk)
        if got != expected:
            raise StorageError(f"分片不完整：收到 {got} 字节，应为 {expected}")
        return md5.hexdigest()

    def partial_exists(self, key: str) -> bool:
        return self._partial(key).exists()

    def complete(self, key: str) -> None:
        self._partial(key).replace(self.path(key))

    def abort(self, key: str) -> None:
        self._partial(key).unlink(missing_ok=True)

    # ---- 普通读写 ----
    def save_path(self, key: str) -> Path:
        """服务器自己生成的文件（如提取的音频）写到这里。"""
        p = self.path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    def exists(self, key: str) -> bool:
        return self.path(key).exists()

    def delete(self, key: str) -> None:
        self.path(key).unlink(missing_ok=True)


def get_storage(root: Path):
    backend = os.environ.get("STORAGE_BACKEND", "local")
    if backend == "local":
        return LocalStorage(root)
    raise StorageError(f"还不支持的存储：{backend}（V2 先用 local，OSS 在买服务器后接入）")

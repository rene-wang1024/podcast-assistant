"""账号密码登录。密码用 scrypt 加盐哈希存储。"""
from __future__ import annotations

import hashlib
import hmac
import secrets

ROLES = {
    "recorder": "录制团队",
    "producer": "制作人",
    "editor": "精剪伙伴",
    "copywriter": "文案伙伴",
    "operator": "运营伙伴",
}


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1)
    return hmac.compare_digest(digest.hex(), digest_hex)


def validate_new_password(password: str) -> str | None:
    if len(password) < 8:
        return "密码至少 8 位"
    return None

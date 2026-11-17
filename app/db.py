"""SQLite 存储。数据和上传文件都放在 DATA_DIR（默认 ./data）。"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
UPLOAD_DIR = DATA_DIR / "uploads"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    display_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL,              -- recorder / producer / editor / copywriter / operator
    is_admin INTEGER NOT NULL DEFAULT 0,
    mobile TEXT DEFAULT '',          -- 企业微信群里 @ 人用的手机号
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS episodes (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL,              -- 期号，如 EP37
    title TEXT NOT NULL,
    current_stage TEXT NOT NULL,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS stages (
    id INTEGER PRIMARY KEY,
    episode_id INTEGER NOT NULL REFERENCES episodes(id),
    key TEXT NOT NULL,
    status TEXT NOT NULL,            -- waiting / todo / done
    assignee_id INTEGER REFERENCES users(id),
    data TEXT NOT NULL DEFAULT '{}', -- 表单字段 JSON
    ai_output TEXT,
    ai_review TEXT,                  -- 审查结果 JSON
    ai_meta TEXT,                    -- 用的模型、时间
    started_at TEXT,
    completed_at TEXT,
    completed_by INTEGER REFERENCES users(id),
    UNIQUE(episode_id, key)
);
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    episode_id INTEGER NOT NULL REFERENCES episodes(id),
    stage_key TEXT NOT NULL,
    field TEXT NOT NULL,
    filename TEXT NOT NULL,
    stored_name TEXT NOT NULL,
    size INTEGER NOT NULL,
    uploaded_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    episode_id INTEGER REFERENCES episodes(id),
    user_id INTEGER REFERENCES users(id),
    action TEXT NOT NULL,
    detail TEXT DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS uploads (       -- 分片上传（大文件）
    id TEXT PRIMARY KEY,
    episode_id INTEGER NOT NULL REFERENCES episodes(id),
    stage_key TEXT NOT NULL,
    field TEXT NOT NULL,
    filename TEXT NOT NULL,
    size INTEGER NOT NULL,
    part_size INTEGER NOT NULL,
    stored_name TEXT NOT NULL,
    status TEXT NOT NULL,            -- uploading / done / aborted
    user_id INTEGER REFERENCES users(id),
    file_id INTEGER REFERENCES files(id),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS upload_parts (
    upload_id TEXT NOT NULL REFERENCES uploads(id),
    n INTEGER NOT NULL,
    etag TEXT NOT NULL,
    PRIMARY KEY (upload_id, n)
);
CREATE TABLE IF NOT EXISTS jobs (          -- 服务器后台处理（提取音频、转写）
    id INTEGER PRIMARY KEY,
    episode_id INTEGER NOT NULL REFERENCES episodes(id),
    kind TEXT NOT NULL,
    file_id INTEGER REFERENCES files(id),
    status TEXT NOT NULL,            -- queued / running / done / failed / skipped
    result TEXT,
    error TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DATA_DIR / "app.db", timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")  # 后台任务写库时，网页还能读
    return conn


MIGRATIONS = [  # (表, 列, 定义)：给老数据库补列
    ("files", "kind", "TEXT NOT NULL DEFAULT 'upload'"),   # upload / derived（服务器生成）
    ("files", "source_file_id", "INTEGER REFERENCES files(id)"),
    ("files", "meta", "TEXT NOT NULL DEFAULT '{}'"),
    ("jobs", "attempts", "INTEGER NOT NULL DEFAULT 0"),
]


def init() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        for table, col, ddl in MIGRATIONS:
            cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if col not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")


def get_setting(conn, key: str, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def set_setting(conn, key: str, value) -> None:
    conn.execute("INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, json.dumps(value, ensure_ascii=False)))


def log(conn, episode_id: int | None, user_id: int | None, action: str, detail: str = "") -> None:
    conn.execute("INSERT INTO events(episode_id, user_id, action, detail, created_at) VALUES(?,?,?,?,?)",
                 (episode_id, user_id, action, detail, now()))

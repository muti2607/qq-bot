"""SQLite 数据访问层：任务、目标、图片、日志、设置。"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "bot.db"

_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL DEFAULT '',
    enabled     INTEGER NOT NULL DEFAULT 1,
    content     TEXT    NOT NULL DEFAULT '',   -- 纯文本模式正文，支持 {date} {time} {weekday} {name}
    image_id    INTEGER,                       -- 图片库中的图片 id
    rss_url     TEXT    NOT NULL DEFAULT '',
    source_mode TEXT    NOT NULL DEFAULT 'static',  -- static | rss_text | rss_with_link
    at_all      INTEGER NOT NULL DEFAULT 0,
    targets     TEXT    NOT NULL DEFAULT '[]', -- JSON: [{"type":"group","id":123}]
    schedule    TEXT    NOT NULL DEFAULT '',   -- cron 表达式, 如 "0 9 * * *"
    timezone    TEXT    NOT NULL DEFAULT 'Asia/Shanghai',
    created_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS images (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    filename    TEXT NOT NULL,        -- 存储文件名
    original    TEXT NOT NULL DEFAULT '',
    size        INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS logs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    level       TEXT NOT NULL DEFAULT 'info',
    task_id     INTEGER,
    message     TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


@contextmanager
def db():
    with _lock:
        conn = _connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def init_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.executescript(SCHEMA)


# ---------------- settings ----------------

DEFAULT_SETTINGS = {
    "onebot_http": "http://127.0.0.1:3001",
    "onebot_token": "",
    "admin_password": "",
    "rss_timeout": "15",
}


def get_settings() -> dict[str, str]:
    with db() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    data = dict(DEFAULT_SETTINGS)
    data.update({r["key"]: r["value"] for r in rows})
    return data


def set_settings(values: dict[str, Any]) -> None:
    with db() as conn:
        for k, v in values.items():
            conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (k, "" if v is None else str(v)),
            )


# ---------------- tasks ----------------


def _task_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    try:
        d["targets"] = json.loads(d.get("targets") or "[]")
    except json.JSONDecodeError:
        d["targets"] = []
    d["enabled"] = bool(d["enabled"])
    d["at_all"] = bool(d["at_all"])
    return d


def list_tasks() -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute("SELECT * FROM tasks ORDER BY id").fetchall()
    return [_task_row_to_dict(r) for r in rows]


def get_task(task_id: int) -> dict[str, Any] | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    return _task_row_to_dict(row) if row else None


TASK_FIELDS = (
    "name",
    "enabled",
    "content",
    "image_id",
    "rss_url",
    "source_mode",
    "at_all",
    "targets",
    "schedule",
    "timezone",
)


def create_task(data: dict[str, Any]) -> int:
    values = {k: data.get(k) for k in TASK_FIELDS}
    values["enabled"] = int(bool(values.get("enabled", True)))
    values["at_all"] = int(bool(values.get("at_all", False)))
    values["targets"] = json.dumps(values.get("targets") or [], ensure_ascii=False)
    values["name"] = values.get("name") or ""
    values["content"] = values.get("content") or ""
    values["rss_url"] = values.get("rss_url") or ""
    values["source_mode"] = values.get("source_mode") or "static"
    values["schedule"] = values.get("schedule") or "0 9 * * *"
    values["timezone"] = values.get("timezone") or "Asia/Shanghai"
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO tasks(name, enabled, content, image_id, rss_url, source_mode, "
            "at_all, targets, schedule, timezone) VALUES(?,?,?,?,?,?,?,?,?,?)",
            tuple(values[k] for k in TASK_FIELDS),
        )
        return int(cur.lastrowid)


def update_task(task_id: int, data: dict[str, Any]) -> None:
    fields, values = [], []
    for k in TASK_FIELDS:
        if k not in data:
            continue
        v = data[k]
        if k in ("enabled", "at_all"):
            v = int(bool(v))
        elif k == "targets":
            v = json.dumps(v or [], ensure_ascii=False)
        elif k == "image_id":
            v = v if v else None
        elif v is None:
            v = ""
        fields.append(f"{k}=?")
        values.append(v)
    if not fields:
        return
    fields.append("updated_at=datetime('now','localtime')")
    values.append(task_id)
    with db() as conn:
        conn.execute(f"UPDATE tasks SET {', '.join(fields)} WHERE id=?", tuple(values))


def delete_task(task_id: int) -> None:
    with db() as conn:
        conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))


# ---------------- images ----------------


def list_images() -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute("SELECT * FROM images ORDER BY id DESC").fetchall()
    return [dict(r) for r in rows]


def add_image(filename: str, original: str, size: int) -> int:
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO images(filename, original, size) VALUES(?,?,?)",
            (filename, original, size),
        )
        return int(cur.lastrowid)


def get_image(image_id: int) -> dict[str, Any] | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM images WHERE id=?", (image_id,)).fetchone()
    return dict(row) if row else None


def delete_image(image_id: int) -> None:
    with db() as conn:
        conn.execute("UPDATE tasks SET image_id=NULL WHERE image_id=?", (image_id,))
        conn.execute("DELETE FROM images WHERE id=?", (image_id,))


# ---------------- logs ----------------


def add_log(message: str, level: str = "info", task_id: int | None = None) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO logs(level, task_id, message) VALUES(?,?,?)",
            (level, task_id, message),
        )
        # 只保留最近 5000 条
        conn.execute(
            "DELETE FROM logs WHERE id <= (SELECT id FROM logs ORDER BY id DESC LIMIT 1 OFFSET 5000)"
        )


def list_logs(limit: int = 200) -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM logs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def clear_logs() -> None:
    with db() as conn:
        conn.execute("DELETE FROM logs")


def prune_old_logs(days: int = 30) -> None:
    with db() as conn:
        conn.execute(
            "DELETE FROM logs WHERE created_at < datetime('now','localtime', ?)",
            (f"-{int(days)} days",),
        )

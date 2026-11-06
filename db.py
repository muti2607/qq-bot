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
    image_mode  TEXT    NOT NULL DEFAULT 'fixed',  -- none | fixed | random
    image_ids   TEXT    NOT NULL DEFAULT '[]', -- random 模式候选图片 id 列表（空=全库随机）
    vars        TEXT    NOT NULL DEFAULT '[]', -- 自定义占位符 [{"key":"赛季","value":"UC"}]
    rss_url     TEXT    NOT NULL DEFAULT '',
    source_mode TEXT    NOT NULL DEFAULT 'static',  -- static | rss_text | rss_with_link
    at_all      INTEGER NOT NULL DEFAULT 0,
    targets     TEXT    NOT NULL DEFAULT '[]', -- JSON: [{"type":"group","id":123}]
    schedule    TEXT    NOT NULL DEFAULT '',   -- cron 表达式, 如 "0 9 * * *"
    timezone    TEXT    NOT NULL DEFAULT 'Asia/Shanghai',
    created_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS triggers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword     TEXT    NOT NULL,
    reply_text  TEXT    NOT NULL DEFAULT '',
    image_mode  TEXT    NOT NULL DEFAULT 'none',   -- none | fixed | random
    image_id    INTEGER,
    image_ids   TEXT    NOT NULL DEFAULT '[]',
    at_required INTEGER NOT NULL DEFAULT 1,        -- 必须 @机器人 才触发
    scope       TEXT    NOT NULL DEFAULT 'all',    -- all | group:123,456 | private
    cooldown    INTEGER NOT NULL DEFAULT 10,       -- 同一会话同一触发词的最小间隔秒数
    quote_mode  TEXT    NOT NULL DEFAULT 'off',    -- off | replace | append（随机名言）
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
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

CREATE TABLE IF NOT EXISTS presets (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    name     TEXT NOT NULL,
    targets  TEXT NOT NULL DEFAULT '[]',  -- JSON: [{"type":"group","id":123,"name":"xx"}]
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
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
        _migrate(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """给已存在的旧表补新增列。"""
    def cols(table: str) -> set[str]:
        return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}

    task_cols = cols("tasks")
    for name, ddl in (
        ("image_mode", "TEXT NOT NULL DEFAULT 'fixed'"),
        ("image_ids", "TEXT NOT NULL DEFAULT '[]'"),
        ("vars", "TEXT NOT NULL DEFAULT '[]'"),
    ):
        if name not in task_cols:
            conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {ddl}")
    trig_cols = cols("triggers")
    if "quote_mode" not in trig_cols:
        conn.execute("ALTER TABLE triggers ADD COLUMN quote_mode TEXT NOT NULL DEFAULT 'off'")


# ---------------- settings ----------------

DEFAULT_SETTINGS = {
    "onebot_http": "http://127.0.0.1:3001",
    "onebot_token": "",
    "post_token": "",
    "admin_password": "",
    "rss_timeout": "15",
}

from quotes_data import QUOTES as DEFAULT_QUOTES



def get_quotes() -> list[str]:
    raw = get_settings().get("quotes", "")
    if raw.strip():
        try:
            data = json.loads(raw)
            if isinstance(data, list) and data:
                return [str(x) for x in data]
        except json.JSONDecodeError:
            pass
    return list(DEFAULT_QUOTES)


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
    for key in ("targets", "image_ids", "vars"):
        try:
            d[key] = json.loads(d.get(key) or "[]")
        except json.JSONDecodeError:
            d[key] = []
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
    "image_mode",
    "image_ids",
    "vars",
    "rss_url",
    "source_mode",
    "at_all",
    "targets",
    "schedule",
    "timezone",
)

_JSON_FIELDS = {"targets", "image_ids", "vars"}
_BOOL_FIELDS = {"enabled", "at_all"}


def _normalize_task(data: dict[str, Any]) -> dict[str, Any]:
    values = {k: data.get(k) for k in TASK_FIELDS}
    for k in _JSON_FIELDS:
        v = values.get(k)
        if v is None:
            v = []
        values[k] = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
    for k in _BOOL_FIELDS:
        values[k] = int(bool(values.get(k, True)))
    defaults = {
        "name": "", "content": "", "rss_url": "",
        "source_mode": "static", "image_mode": "fixed",
        "schedule": "0 9 * * *", "timezone": "Asia/Shanghai",
    }
    for k, v in defaults.items():
        if values.get(k) in (None, ""):
            values[k] = v
    if values.get("image_id") in (0, ""):
        values["image_id"] = None
    return values


def create_task(data: dict[str, Any]) -> int:
    values = _normalize_task(data)
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO tasks(name, enabled, content, image_id, image_mode, image_ids, vars, "
            "rss_url, source_mode, at_all, targets, schedule, timezone) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            tuple(values[k] for k in TASK_FIELDS),
        )
        return int(cur.lastrowid)


def update_task(task_id: int, data: dict[str, Any]) -> None:
    current = get_task(task_id)
    if current is None:
        return
    merged = {k: current.get(k) for k in TASK_FIELDS}
    merged.update({k: v for k, v in data.items() if k in TASK_FIELDS})
    values = _normalize_task(merged)
    fields, params = [], []
    for k in TASK_FIELDS:
        fields.append(f"{k}=?")
        params.append(values[k])
    fields.append("updated_at=datetime('now','localtime')")
    params.append(task_id)
    with db() as conn:
        conn.execute(f"UPDATE tasks SET {', '.join(fields)} WHERE id=?", tuple(params))


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


# ---------------- presets（群组预设） ----------------


def list_presets() -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute("SELECT * FROM presets ORDER BY id").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["targets"] = json.loads(d.get("targets") or "[]")
        except json.JSONDecodeError:
            d["targets"] = []
        out.append(d)
    return out


def create_preset(name: str, targets: list[dict]) -> int:
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO presets(name, targets) VALUES(?,?)",
            (name.strip(), json.dumps(targets, ensure_ascii=False)),
        )
        return int(cur.lastrowid)


def update_preset(pid: int, name: str, targets: list[dict]) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE presets SET name=?, targets=? WHERE id=?",
            (name.strip(), json.dumps(targets, ensure_ascii=False), pid),
        )


def delete_preset(pid: int) -> None:
    with db() as conn:
        conn.execute("DELETE FROM presets WHERE id=?", (pid,))


# ---------------- triggers（@触发回复） ----------------


def _trigger_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    for key in ("image_ids",):
        try:
            d[key] = json.loads(d.get(key) or "[]")
        except json.JSONDecodeError:
            d[key] = []
    d["enabled"] = bool(d["enabled"])
    d["at_required"] = bool(d["at_required"])
    return d


def list_triggers() -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute("SELECT * FROM triggers ORDER BY id").fetchall()
    return [_trigger_row_to_dict(r) for r in rows]


def get_trigger(tid: int) -> dict[str, Any] | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM triggers WHERE id=?", (tid,)).fetchone()
    return _trigger_row_to_dict(row) if row else None


TRIGGER_FIELDS = ("keyword", "reply_text", "image_mode", "image_id", "image_ids",
                  "at_required", "scope", "cooldown", "quote_mode", "enabled")


def _trigger_values(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "keyword": (data.get("keyword") or "").strip(),
        "reply_text": data.get("reply_text") or "",
        "image_mode": data.get("image_mode") or "none",
        "image_id": data.get("image_id") or None,
        "image_ids": json.dumps(data.get("image_ids") or [], ensure_ascii=False),
        "at_required": int(bool(data.get("at_required", True))),
        "scope": data.get("scope") or "all",
        "cooldown": int(data.get("cooldown") or 0),
        "quote_mode": data.get("quote_mode") or "off",
        "enabled": int(bool(data.get("enabled", True))),
    }


def create_trigger(data: dict[str, Any]) -> int:
    v = _trigger_values(data)
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO triggers(keyword, reply_text, image_mode, image_id, image_ids, "
            "at_required, scope, cooldown, quote_mode, enabled) VALUES(?,?,?,?,?,?,?,?,?,?)",
            tuple(v[k] for k in TRIGGER_FIELDS),
        )
        return int(cur.lastrowid)


def update_trigger(tid: int, data: dict[str, Any]) -> None:
    v = _trigger_values(data)
    with db() as conn:
        conn.execute(
            "UPDATE triggers SET keyword=?, reply_text=?, image_mode=?, image_id=?, "
            "image_ids=?, at_required=?, scope=?, cooldown=?, quote_mode=?, enabled=? WHERE id=?",
            (*[v[k] for k in TRIGGER_FIELDS], tid),
        )


def delete_trigger(tid: int) -> None:
    with db() as conn:
        conn.execute("DELETE FROM triggers WHERE id=?", (tid,))


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

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
    at_reply    INTEGER NOT NULL DEFAULT 1,        -- 回复时是否 @ 触发者
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
    status      TEXT NOT NULL DEFAULT 'approved',  -- pending | approved | rejected
    uploader    TEXT NOT NULL DEFAULT '',          -- 上传者账号（远程上传时记录）
    review_note TEXT NOT NULL DEFAULT '',          -- 审核备注/拒绝原因
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS apply_requests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    mail        TEXT NOT NULL,
    username    TEXT NOT NULL,
    password    TEXT NOT NULL,        -- sha256 hex
    status      TEXT NOT NULL DEFAULT 'pending',  -- pending | approved | rejected
    note        TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS upload_users (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    username    TEXT NOT NULL UNIQUE,
    password    TEXT NOT NULL,        -- sha256 hex
    uid_number  TEXT UNIQUE,          -- 5-9 位纯数字登录ID
    note        TEXT NOT NULL DEFAULT '',
    enabled     INTEGER NOT NULL DEFAULT 1,
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
    if "at_reply" not in trig_cols:
        conn.execute("ALTER TABLE triggers ADD COLUMN at_reply INTEGER NOT NULL DEFAULT 1")
    img_cols = cols("images")
    for name, ddl in (
        ("status", "TEXT NOT NULL DEFAULT 'approved'"),
        ("uploader", "TEXT NOT NULL DEFAULT ''"),
        ("review_note", "TEXT NOT NULL DEFAULT ''"),
    ):
        if name not in img_cols:
            conn.execute(f"ALTER TABLE images ADD COLUMN {name} {ddl}")
    uu_cols = cols("upload_users")
    if "uid_number" not in uu_cols:
        conn.execute("ALTER TABLE upload_users ADD COLUMN uid_number TEXT")
    # 给没有数字ID的账号补分配
    import random as _random
    used = {r["uid_number"] for r in conn.execute(
        "SELECT uid_number FROM upload_users WHERE uid_number IS NOT NULL").fetchall()}
    for row in conn.execute("SELECT id FROM upload_users WHERE uid_number IS NULL").fetchall():
        while True:
            num = str(_random.randint(10000, 999999999))
            if num not in used:
                break
        used.add(num)
        conn.execute("UPDATE upload_users SET uid_number=? WHERE id=?", (num, row["id"]))


# ---------------- settings ----------------

DEFAULT_SETTINGS = {
    "onebot_http": "http://127.0.0.1:3001",
    "onebot_token": "",
    "post_token": "",
    "remote_token": "",
    "smtp_host": "smtp.qq.com",
    "smtp_port": "465",
    "smtp_user": "",
    "smtp_pass": "",
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


def list_images(only_approved: bool = False, status: str | None = None,
                uploader: str | None = None) -> list[dict[str, Any]]:
    clauses, args = [], []
    if only_approved:
        clauses.append("status='approved'")
    elif status:
        clauses.append("status=?")
        args.append(status)
    if uploader:
        clauses.append("uploader=?")
        args.append(uploader)
    sql = "SELECT * FROM images" + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY id DESC"
    with db() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def add_image(filename: str, original: str, size: int,
              status: str = "approved", uploader: str = "") -> int:
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO images(filename, original, size, status, uploader) VALUES(?,?,?,?,?)",
            (filename, original, size, status, uploader),
        )
        return int(cur.lastrowid)


def review_image(image_id: int, status: str, note: str = "") -> bool:
    """审核图片：status=pending/approved/rejected。"""
    if status not in ("pending", "approved", "rejected"):
        return False
    with db() as conn:
        cur = conn.execute(
            "UPDATE images SET status=?, review_note=? WHERE id=?",
            (status, note, image_id),
        )
        return cur.rowcount > 0


def bulk_review(ids: list[int], status: str, note: str = "") -> int:
    if status not in ("pending", "approved", "rejected") or not ids:
        return 0
    with db() as conn:
        cur = conn.execute(
            f"UPDATE images SET status=?, review_note=? WHERE id IN ({','.join('?' * len(ids))})",
            (status, note, *ids),
        )
        return cur.rowcount


def get_image(image_id: int) -> dict[str, Any] | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM images WHERE id=?", (image_id,)).fetchone()
    return dict(row) if row else None


def delete_image(image_id: int) -> None:
    with db() as conn:
        conn.execute("UPDATE tasks SET image_id=NULL WHERE image_id=?", (image_id,))
        conn.execute("DELETE FROM images WHERE id=?", (image_id,))


# ---------------- upload_users（远程上传账号） ----------------


def _hash_pw(pw: str) -> str:
    import hashlib
    return hashlib.sha256(("qqbot-upload:" + pw).encode()).hexdigest()


def list_upload_users() -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute("SELECT * FROM upload_users ORDER BY id").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d.pop("password", None)
        d["enabled"] = bool(d["enabled"])
        out.append(d)
    return out


def get_upload_user(username: str) -> dict[str, Any] | None:
    """按用户名或 5-9 位纯数字ID查找（数字ID也可用于登录）。"""
    with db() as conn:
        row = conn.execute("SELECT * FROM upload_users WHERE username=?", (username,)).fetchone()
        if not row and username.isdigit() and 5 <= len(username) <= 9:
            row = conn.execute("SELECT * FROM upload_users WHERE uid_number=?", (username,)).fetchone()
    return dict(row) if row else None


def verify_upload_user(username: str, password: str) -> bool:
    u = get_upload_user(username)
    if not u or not u["enabled"]:
        return False
    import secrets
    return secrets.compare_digest(u["password"], _hash_pw(password))


def alloc_uid_number(conn: sqlite3.Connection) -> str:
    """分配一个不冲突的 5-9 位纯数字ID。"""
    import random
    while True:
        num = str(random.randint(10000, 999999999))
        row = conn.execute("SELECT 1 FROM upload_users WHERE uid_number=?", (num,)).fetchone()
        if not row:
            return num


def create_upload_user(username: str, password: str, note: str = "") -> int | None:
    try:
        with db() as conn:
            num = alloc_uid_number(conn)
            cur = conn.execute(
                "INSERT INTO upload_users(username, password, uid_number, note, enabled) VALUES(?,?,?,?,1)",
                (username.strip(), _hash_pw(password), num, note.strip()),
            )
            return int(cur.lastrowid)
    except sqlite3.IntegrityError:
        return None


def update_upload_user(uid: int, password: str | None = None,
                       note: str | None = None, enabled: bool | None = None) -> None:
    fields, args = [], []
    if password:
        fields.append("password=?")
        args.append(_hash_pw(password))
    if note is not None:
        fields.append("note=?")
        args.append(note.strip())
    if enabled is not None:
        fields.append("enabled=?")
        args.append(int(bool(enabled)))
    if not fields:
        return
    args.append(uid)
    with db() as conn:
        conn.execute(f"UPDATE upload_users SET {', '.join(fields)} WHERE id=?", args)


def delete_upload_user(uid: int) -> None:
    with db() as conn:
        conn.execute("DELETE FROM upload_users WHERE id=?", (uid,))


# ---------------- apply_requests（注册申请） ----------------


def create_apply(mail: str, username: str, password: str) -> int | None:
    try:
        with db() as conn:
            cur = conn.execute(
                "INSERT INTO apply_requests(mail, username, password, status) VALUES(?,?,?,'pending')",
                (mail.strip().lower(), username.strip(), _hash_pw(password)),
            )
            return int(cur.lastrowid)
    except sqlite3.IntegrityError:
        return None  # 用户名已存在


def list_applies(status: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM apply_requests" + (" WHERE status=?" if status else "") + " ORDER BY id DESC"
    with db() as conn:
        rows = conn.execute(sql, (status,) if status else ()).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d.pop("password", None)
        out.append(d)
    return out


def get_apply(aid: int) -> dict[str, Any] | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM apply_requests WHERE id=?", (aid,)).fetchone()
    return dict(row) if row else None


def set_apply_status(aid: int, status: str) -> None:
    with db() as conn:
        conn.execute("UPDATE apply_requests SET status=? WHERE id=?", (status, aid))


def approve_apply(aid: int) -> int | None:
    """批准申请：创建上传账号并标记 approved。返回账号 id，用户名冲突返回 None。

    注意：apply.password 已是哈希，直接入库，不能再哈希一次。
    """
    a = get_apply(aid)
    if not a or a["status"] != "pending":
        return None
    try:
        with db() as conn:
            num = alloc_uid_number(conn)
            cur = conn.execute(
                "INSERT INTO upload_users(username, password, uid_number, note, enabled) VALUES(?,?,?,?,1)",
                (a["username"], a["password"], num, f"邮箱注册 {a['mail']}"),
            )
            uid = int(cur.lastrowid)
    except sqlite3.IntegrityError:
        return None
    set_apply_status(aid, "approved")
    return uid


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
    d["at_reply"] = bool(d.get("at_reply", 1))
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
                  "at_required", "at_reply", "scope", "cooldown", "quote_mode", "enabled")


def _trigger_values(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "keyword": (data.get("keyword") or "").strip(),
        "reply_text": data.get("reply_text") or "",
        "image_mode": data.get("image_mode") or "none",
        "image_id": data.get("image_id") or None,
        "image_ids": json.dumps(data.get("image_ids") or [], ensure_ascii=False),
        "at_required": int(bool(data.get("at_required", True))),
        "at_reply": int(bool(data.get("at_reply", True))),
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
            "at_required, at_reply, scope, cooldown, quote_mode, enabled) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            tuple(v[k] for k in TRIGGER_FIELDS),
        )
        return int(cur.lastrowid)


def update_trigger(tid: int, data: dict[str, Any]) -> None:
    v = _trigger_values(data)
    with db() as conn:
        conn.execute(
            "UPDATE triggers SET keyword=?, reply_text=?, image_mode=?, image_id=?, "
            "image_ids=?, at_required=?, at_reply=?, scope=?, cooldown=?, quote_mode=?, enabled=? WHERE id=?",
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

"""SQLite 数据访问层：任务、目标、图片、日志、设置。"""
from __future__ import annotations

import json
import os
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
    hash        TEXT NOT NULL DEFAULT '',          -- 内容哈希（去重用）
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
    is_admin    INTEGER NOT NULL DEFAULT 0,   -- 1=管理员（可审批图片/申请）
    note        TEXT NOT NULL DEFAULT '',
    enabled     INTEGER NOT NULL DEFAULT 1,
    banned_until TEXT NOT NULL DEFAULT '',    -- 封禁到期（空=未封，permanent=永久）
    ban_reason  TEXT NOT NULL DEFAULT '',    -- 封禁原因
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

CREATE TABLE IF NOT EXISTS chat_memory (
    session_key TEXT PRIMARY KEY,             -- g:群号 / p:QQ号
    payload     TEXT NOT NULL DEFAULT '[]',   -- JSON: [{"role":"user","content":"..."}]
    updated_at  REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS stickers (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    filename   TEXT NOT NULL,                 -- data/stickers/xxx.png
    content_hash TEXT NOT NULL DEFAULT '',    -- 图片内容 md5，用于按内容去重
    note       TEXT NOT NULL DEFAULT '',      -- 自己写的备注（这张图什么意思）
    tags       TEXT NOT NULL DEFAULT '',      -- 逗号分隔标签
    usage      TEXT NOT NULL DEFAULT '',      -- 适合什么场景用
    used_count INTEGER NOT NULL DEFAULT 0,    -- 发过几次
    last_used  REAL NOT NULL DEFAULT 0,       -- 上次发送时间戳
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
    st_cols = cols("stickers")
    if "content_hash" not in st_cols:
        conn.execute("ALTER TABLE stickers ADD COLUMN content_hash TEXT NOT NULL DEFAULT ''")
    uu_cols = cols("upload_users")
    if "uid_number" not in uu_cols:
        conn.execute("ALTER TABLE upload_users ADD COLUMN uid_number TEXT")
    if "is_admin" not in uu_cols:
        conn.execute("ALTER TABLE upload_users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0")
    if "banned_until" not in uu_cols:
        conn.execute("ALTER TABLE upload_users ADD COLUMN banned_until TEXT NOT NULL DEFAULT ''")
    if "ban_reason" not in uu_cols:
        conn.execute("ALTER TABLE upload_users ADD COLUMN ban_reason TEXT NOT NULL DEFAULT ''")
    img_cols = cols("images")
    if "hash" not in img_cols:
        conn.execute("ALTER TABLE images ADD COLUMN hash TEXT NOT NULL DEFAULT ''")
    # 为已有图片补算哈希（读文件）
    import hashlib as _hl
    for row in conn.execute("SELECT id, filename, hash FROM images WHERE hash=''").fetchall():
        fpath = UPLOAD_DIR / row["filename"]
        if fpath.exists():
            h = _hl.sha256(fpath.read_bytes()).hexdigest()
            conn.execute("UPDATE images SET hash=? WHERE id=?", (h, row["id"]))
    conn.execute("CREATE INDEX IF NOT EXISTS idx_images_hash ON images(hash)")
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
    # AI 聊天配置（兼容 OpenAI /v1/chat/completions，如 DeepSeek、SiliconFlow、OpenRouter、Ollama 等）
    "ai_enabled": "0",
    "ai_api_base": "",
    "ai_api_key": "",
    "ai_model": "",
    "ai_system_prompt": "你是一个友好的 QQ 机器人助手，用简洁自然的中文回复，不要过长。",
    "ai_reply_mode": "at",   # off | all（all=不用@也回）| at（只回@）
    # 多接口故障转移：每行 `名称|接口地址|模型|Key`
    "ai_providers": "",
    "ai_vision_providers": "",   # 视觉模型专用列表（看图用），留空则复用主接口
    "ai_keyless_fallback": "1",   # 1=启用内置免Key兜底接口
    "ai_at_reply": "0",           # 回复时是否 @ 对方：0=不带（默认）1=带
    "ai_group_gap": "25",         # all 模式下同一群两次回复的最小间隔（秒），防刷屏
    "ai_send_min_gap": "6",       # 全局：任意两条消息最小间隔（秒），防封号
    "ai_send_per_min": "8",       # 全局：每分钟最多发几条，防封号
    "ai_use_face": "1",           # 允许发表情（QQ 小黄脸）
    "ai_use_poke": "1",           # 允许戳一戳
    "ai_use_sticker": "1",        # 允许发自己偷来的表情包
    "ai_see_image": "1",          # 允许看图（视觉模型）
    "ai_save_sticker": "1",       # 允许自己判断并收藏别人的表情
    "ai_fav_to_qq": "1",          # 偷到的表情同时存进 QQ 的收藏表情
    "ai_use_qq_fav": "1",         # 发表情时优先用 QQ 收藏表情
    "ai_send_animated": "1",      # 允许发动图（GIF）
    "ai_poke_reply_poke": "1",    # 被戳时回戳
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


def find_image_by_hash(h: str) -> dict | None:
    """按内容哈希找在库有效图片（pending/approved）。
    rejected（人工驳回）的不算——允许上传者重传修正。"""
    if not h:
        return None
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM images WHERE hash=? AND status IN ('pending','approved') LIMIT 1",
            (h,),
        ).fetchone()
    return dict(row) if row else None


def add_image(filename: str, original: str, size: int,
              status: str = "approved", uploader: str = "", hash: str = "") -> int:
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO images(filename, original, size, status, uploader, hash) VALUES(?,?,?,?,?,?)",
            (filename, original, size, status, uploader, hash),
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


# ---------------- stickers（表情库） ----------------

STICKER_DIR = str(DATA_DIR / "stickers")


def add_sticker(data: bytes, note: str = "", tags: str = "", usage: str = "",
                ext: str = "png") -> int | None:
    """保存一张表情图片，返回 id；同一张图（内容相同）已存在时返回已有 id，不重复存。"""
    import hashlib
    h = hashlib.md5(data).hexdigest()
    with db() as conn:
        row = conn.execute("SELECT id FROM stickers WHERE content_hash=?", (h,)).fetchone()
        if row:
            return int(row["id"])          # 已存在，直接复用

    os.makedirs(str(DATA_DIR / "stickers"), exist_ok=True)
    import secrets as _secrets
    ext = (ext or "png").lower().lstrip(".")
    if ext not in ("png", "gif", "jpg", "jpeg", "webp"):
        ext = "png"
    name = "s-" + _secrets.token_hex(8) + "." + ext
    with open(os.path.join(str(DATA_DIR / "stickers"), name), "wb") as f:
        f.write(data)
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO stickers(filename, content_hash, note, tags, usage) VALUES(?,?,?,?,?)",
            (name, h, note, tags, usage),
        )
        return int(cur.lastrowid)


def find_sticker_by_hash(h: str) -> dict[str, Any] | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM stickers WHERE content_hash=?", (h,)).fetchone()
    return dict(row) if row else None


# ---------------- chat_memory（会话上下文持久化） ----------------


def save_memory(session_key: str, messages: list[dict[str, str]]) -> None:
    """把某个会话的上下文写盘，重启后还能接着聊。"""
    if not session_key:
        return
    try:
        payload = json.dumps(messages[-40:], ensure_ascii=False)
        with db() as conn:
            conn.execute(
                "INSERT INTO chat_memory(session_key, payload, updated_at) VALUES(?,?,?) "
                "ON CONFLICT(session_key) DO UPDATE SET payload=excluded.payload, "
                "updated_at=excluded.updated_at",
                (session_key, payload, time.time()),
            )
    except Exception:
        pass


def load_memory(session_key: str, max_age: float = 7200.0) -> list[dict[str, str]]:
    """读回会话上下文；超过 max_age 秒没更新的丢弃。"""
    if not session_key:
        return []
    try:
        with db() as conn:
            row = conn.execute(
                "SELECT payload, updated_at FROM chat_memory WHERE session_key=?",
                (session_key,)).fetchone()
        if not row:
            return []
        if time.time() - float(row["updated_at"] or 0) > max_age:
            return []
        data = json.loads(row["payload"] or "[]")
        return [m for m in data if isinstance(m, dict) and "role" in m and "content" in m]
    except Exception:
        return []


def drop_memory(session_key: str) -> None:
    try:
        with db() as conn:
            conn.execute("DELETE FROM chat_memory WHERE session_key=?", (session_key,))
    except Exception:
        pass


def sticker_path(sticker: dict) -> str:
    return os.path.join(str(DATA_DIR / "stickers"), sticker["filename"])


def list_stickers(limit: int = 100) -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM stickers ORDER BY used_count DESC, id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_sticker(sid: int) -> dict[str, Any] | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM stickers WHERE id=?", (sid,)).fetchone()
    return dict(row) if row else None


def pick_sticker(exclude_recent: int = 8) -> dict[str, Any] | None:
    """随机挑一张，尽量避开最近发过的。"""
    import random
    with db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM stickers ORDER BY last_used ASC, RANDOM() LIMIT ?", (max(1, exclude_recent),)
        ).fetchall()]
        if not rows:
            return None
        return random.choice(rows)


def touch_sticker(sid: int) -> None:
    import time as _t
    with db() as conn:
        conn.execute(
            "UPDATE stickers SET used_count=used_count+1, last_used=? WHERE id=?",
            (_t.time(), sid),
        )


def set_sticker_note(sid: int, note: str | None = None, tags: str | None = None,
                     usage: str | None = None) -> None:
    sets, args = [], []
    for col, val in (("note", note), ("tags", tags), ("usage", usage)):
        if val is not None:
            sets.append(f"{col}=?")
            args.append(val)
    if not sets:
        return
    with db() as conn:
        conn.execute(f"UPDATE stickers SET {','.join(sets)} WHERE id=?", (*args, sid))


def delete_sticker(sid: int) -> None:
    st = get_sticker(sid)
    if st:
        try:
            os.remove(sticker_path(st))
        except OSError:
            pass
    with db() as conn:
        conn.execute("DELETE FROM stickers WHERE id=?", (sid,))


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
        d["is_admin"] = bool(d.get("is_admin"))
        banned, until, reason = ban_status(d)
        d["banned"] = banned
        d["banned_until"] = until
        d["ban_reason"] = reason
        out.append(d)
    return out


def get_upload_user_by_id(uid: int) -> dict[str, Any] | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM upload_users WHERE id=?", (uid,)).fetchone()
    return dict(row) if row else None


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


def create_upload_user(username: str, password: str, note: str = "",
                       is_admin: bool = False) -> int | None:
    try:
        with db() as conn:
            num = alloc_uid_number(conn)
            cur = conn.execute(
                "INSERT INTO upload_users(username, password, uid_number, is_admin, note, enabled) "
                "VALUES(?,?,?,?,?,1)",
                (username.strip(), _hash_pw(password), num, int(bool(is_admin)), note.strip()),
            )
            return int(cur.lastrowid)
    except sqlite3.IntegrityError:
        return None


def rename_upload_user(uid: int, new_name: str) -> bool:
    """改账号名。成功返回 True，重名返回 False。"""
    new_name = new_name.strip()
    if not new_name:
        return False
    try:
        with db() as conn:
            conn.execute("UPDATE upload_users SET username=? WHERE id=?", (new_name, uid))
            return True
    except sqlite3.IntegrityError:
        return False


def update_upload_user(uid: int, password: str | None = None,
                       note: str | None = None, enabled: bool | None = None,
                       is_admin: bool | None = None) -> None:
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
    if is_admin is not None:
        fields.append("is_admin=?")
        args.append(int(bool(is_admin)))
    if not fields:
        return
    args.append(uid)
    with db() as conn:
        conn.execute(f"UPDATE upload_users SET {', '.join(fields)} WHERE id=?", args)


def delete_upload_user(uid: int) -> None:
    with db() as conn:
        conn.execute("DELETE FROM upload_users WHERE id=?", (uid,))


# ---------------- 封禁 ----------------

def ban_user(uid: int, until: str, reason: str = "") -> None:
    """封禁账号。until 为空字符串或 'permanent' 表示永久封禁。"""
    val = "permanent" if not until or until == "permanent" else until
    with db() as conn:
        conn.execute(
            "UPDATE upload_users SET banned_until=?, ban_reason=? WHERE id=?",
            (val, reason[:120], uid),
        )


def unban_user(uid: int) -> None:
    with db() as conn:
        conn.execute("UPDATE upload_users SET banned_until='', ban_reason='' WHERE id=?", (uid,))


def ban_status(u: dict) -> tuple:
    """返回 (是否封禁中, 到期时间文本, 原因)。已过期的自动解封。"""
    import datetime as _dt
    raw = (u.get("banned_until") or "").strip()
    if not raw:
        return False, "", ""
    if raw != "permanent":
        try:
            until = _dt.datetime.strptime(raw, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return False, "", ""
        if until <= _dt.datetime.now():
            unban_user(int(u["id"]))
            return False, "", ""
        return True, raw, u.get("ban_reason") or ""
    return True, "永久", u.get("ban_reason") or ""



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

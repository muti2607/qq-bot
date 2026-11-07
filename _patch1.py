# -*- coding: utf-8 -*-
"""给上传账号添加 5-9 位纯数字 ID：可登录、可作标记。

设计：
- upload_users 加 uid_number 列（唯一，5-9位纯数字字符串）
- 创建账号时自动分配（随机 5-9 位，不与现有冲突）
- 旧账号迁移时补分配
- 登录支持：用户名或数字ID + 密码
- 图片记录 uploader（存数字ID），显示时「@数字ID」
"""
import sys
sys.stdout.reconfigure(encoding="utf-8")

# ============ 1. db.py 修改 ============
db_src = open("db.py", encoding="utf-8").read()

# 1a. schema: upload_users 加 uid_number 列
old = """CREATE TABLE IF NOT EXISTS upload_users (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    username    TEXT NOT NULL UNIQUE,
    password    TEXT NOT NULL,        -- sha256 hex
    note        TEXT NOT NULL DEFAULT '',
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);"""
new = """CREATE TABLE IF NOT EXISTS upload_users (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    username    TEXT NOT NULL UNIQUE,
    password    TEXT NOT NULL,        -- sha256 hex
    uid_number  TEXT UNIQUE,          -- 5-9 位纯数字登录ID
    note        TEXT NOT NULL DEFAULT '',
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);"""
assert old in db_src
db_src = db_src.replace(old, new)

# 1b. 迁移：补列 + 给老账号分配数字ID
old_mig = '''    img_cols = cols("images")
    for name, ddl in (
        ("status", "TEXT NOT NULL DEFAULT 'approved'"),
        ("uploader", "TEXT NOT NULL DEFAULT ''"),
        ("review_note", "TEXT NOT NULL DEFAULT ''"),
    ):
        if name not in img_cols:
            conn.execute(f"ALTER TABLE images ADD COLUMN {name} {ddl}")'''
new_mig = '''    img_cols = cols("images")
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
        conn.execute("UPDATE upload_users SET uid_number=? WHERE id=?", (num, row["id"]))'''
assert old_mig in db_src
db_src = db_src.replace(old_mig, new_mig)

# 1c. create_upload_user 自动分配数字ID
old_cu = '''def create_upload_user(username: str, password: str, note: str = "") -> int | None:
    try:
        with db() as conn:
            cur = conn.execute(
                "INSERT INTO upload_users(username, password, note, enabled) VALUES(?,?,?,1)",
                (username.strip(), _hash_pw(password), note.strip()),
            )
            return int(cur.lastrowid)
    except sqlite3.IntegrityError:
        return None'''
new_cu = '''def alloc_uid_number(conn: sqlite3.Connection) -> str:
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
        return None'''
assert old_cu in db_src
db_src = db_src.replace(old_cu, new_cu)

# 1d. get_upload_user 支持用户名或数字ID登录
old_gu = '''def get_upload_user(username: str) -> dict[str, Any] | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM upload_users WHERE username=?", (username,)).fetchone()
    return dict(row) if row else None'''
new_gu = '''def get_upload_user(username: str) -> dict[str, Any] | None:
    """按用户名或 5-9 位纯数字ID查找（数字ID也可用于登录）。"""
    with db() as conn:
        row = conn.execute("SELECT * FROM upload_users WHERE username=?", (username,)).fetchone()
        if not row and username.isdigit() and 5 <= len(username) <= 9:
            row = conn.execute("SELECT * FROM upload_users WHERE uid_number=?", (username,)).fetchone()
    return dict(row) if row else None'''
assert old_gu in db_src
db_src = db_src.replace(old_gu, new_gu)

# 1e. list_upload_users 返回 uid_number
old_lu = '''def list_upload_users() -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute("SELECT * FROM upload_users ORDER BY id").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d.pop("password", None)
        d["enabled"] = bool(d["enabled"])
        out.append(d)
    return out'''
new_lu = '''def list_upload_users() -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute("SELECT * FROM upload_users ORDER BY id").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d.pop("password", None)
        d["enabled"] = bool(d["enabled"])
        out.append(d)
    return out'''
assert old_lu in db_src  # 内容不变，uid_number 自然带出

# 1f. approve_apply 也分配数字ID（沿用 create_upload_user 逻辑不行——它自己哈希）
old_aa = '''    try:
        with db() as conn:
            cur = conn.execute(
                "INSERT INTO upload_users(username, password, note, enabled) VALUES(?,?,?,1)",
                (a["username"], a["password"], f"邮箱注册 {a['mail']}"),
            )
            uid = int(cur.lastrowid)'''
new_aa = '''    try:
        with db() as conn:
            num = alloc_uid_number(conn)
            cur = conn.execute(
                "INSERT INTO upload_users(username, password, uid_number, note, enabled) VALUES(?,?,?,?,1)",
                (a["username"], a["password"], num, f"邮箱注册 {a['mail']}"),
            )
            uid = int(cur.lastrowid)'''
assert old_aa in db_src
db_src = db_src.replace(old_aa, new_aa)

open("db.py", "w", encoding="utf-8").write(db_src)
import ast
ast.parse(db_src)
print("db.py: uid_number added")

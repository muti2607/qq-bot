# -*- coding: utf-8 -*-
"""签到积分系统：
- 每日首次签到 +10 分，连续签到奖励（7天+50，30天+200）
- 积分兑换管理员权限（多档：100分=1天，500分=7天，1500分=30天）
- 到期自动收回（惰性检查）
"""
import sqlite3
from typing import Any

from db import db

# 签到分值
DAILY_POINTS = 10
STREAK_BONUS = {7: 50, 30: 200}   # 连续 N 天额外奖励

# 兑换档位：积分 → 管理员天数
EXCHANGE_TIERS = [
    {"points": 100, "days": 1, "label": "1 天"},
    {"points": 500, "days": 7, "label": "7 天"},
    {"points": 1500, "days": 30, "label": "30 天"},
]


def _migrate(conn: sqlite3.Connection) -> None:
    """签到相关列（挂在 upload_users 上）。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(upload_users)").fetchall()}
    if "points" not in cols:
        conn.execute("ALTER TABLE upload_users ADD COLUMN points INTEGER NOT NULL DEFAULT 0")
    if "last_checkin" not in cols:
        conn.execute("ALTER TABLE upload_users ADD COLUMN last_checkin TEXT NOT NULL DEFAULT ''")
    if "streak" not in cols:
        conn.execute("ALTER TABLE upload_users ADD COLUMN streak INTEGER NOT NULL DEFAULT 0")
    if "admin_until" not in cols:
        conn.execute("ALTER TABLE upload_users ADD COLUMN admin_until TEXT NOT NULL DEFAULT ''")


def _now() -> str:
    import datetime
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _today() -> str:
    import datetime
    return datetime.datetime.now().strftime("%Y-%m-%d")


def _yesterday() -> str:
    import datetime
    return (datetime.datetime.now() - datetime.timedelta(days=1)).strftime("%Y-%m-%d")


def checkin(username: str) -> dict:
    """每日签到。返回 {points, streak, bonus, already}。"""
    u = _get_user(username)
    if not u:
        return {"ok": False, "msg": "账号不存在"}
    today = _today()
    if u.get("last_checkin") == today:
        return {"ok": False, "already": True,
                "msg": "今天已经签到过了，明天再来",
                "points": u.get("points", 0), "streak": u.get("streak", 0)}

    # 连续判定
    streak = (u.get("streak") or 0) + 1 if u.get("last_checkin") == _yesterday() else 1
    gain = DAILY_POINTS
    bonus = 0
    for days, extra in STREAK_BONUS.items():
        if streak > 0 and streak % days == 0:
            bonus += extra
    gain += bonus
    # 活动倍率（取当前最高生效倍率）
    mult = current_multiplier()
    if mult > 1.0:
        gain = int(gain * mult)

    with db() as conn:
        conn.execute(
            "UPDATE upload_users SET points=points+?, last_checkin=?, streak=? WHERE username=?",
            (gain, today, streak, username),
        )
    u2 = _get_user(username)
    import db as _dbmod
    _dbmod.add_point_log(username, gain, u2.get("points", 0),
                  "每日签到" + (f"（连续{streak}天奖励+{bonus}）" if bonus else "")
                  + (f"（活动{mult:g}倍）" if mult > 1.0 else ""))
    msg = f"签到成功 +{gain} 分"
    if mult > 1.0:
        msg += f"（活动 {mult:g} 倍）"
    if bonus:
        msg += f"，含连续 {streak} 天奖励 +{bonus} 分"
    return {"ok": True, "msg": msg, "points": u2["points"],
            "streak": streak, "gain": gain, "bonus": bonus}


def _get_user(username: str) -> dict | None:
    with db() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM upload_users WHERE username=?", (username,)).fetchone()
    return dict(row) if row else None


def get_points(username: str) -> dict:
    """积分 + 管理员状态概览。"""
    u = _get_user(username)
    if not u:
        return {"ok": False}
    import datetime as _dt
    admin_until = (u.get("admin_until") or "").strip()
    is_timed_admin = False
    if admin_until:
        try:
            if _dt.datetime.strptime(admin_until, "%Y-%m-%d %H:%M:%S") > _dt.datetime.now():
                is_timed_admin = True
            else:
                # 到期自动收回
                with db() as conn:
                    conn.execute("UPDATE upload_users SET admin_until='' WHERE username=?", (username,))
                admin_until = ""
        except ValueError:
            pass
    return {"ok": True, "points": u.get("points", 0), "streak": u.get("streak", 0),
            "last_checkin": u.get("last_checkin", ""),
            "is_admin": bool(u.get("is_admin")) or is_timed_admin,
            "is_timed_admin": is_timed_admin, "admin_until": admin_until,
            "today_checked": u.get("last_checkin") == _today(),
            "tiers": EXCHANGE_TIERS}


def exchange(username: str, tier_index: int) -> dict:
    """积分兑换管理员权限。"""
    if not (0 <= tier_index < len(EXCHANGE_TIERS)):
        return {"ok": False, "msg": "无效的兑换档位"}
    tier = EXCHANGE_TIERS[tier_index]
    u = _get_user(username)
    if not u:
        return {"ok": False, "msg": "账号不存在"}
    if (u.get("points") or 0) < tier["points"]:
        return {"ok": False, "msg": f"积分不足（需要 {tier['points']}，当前 {u['points']}）"}

    import datetime as _dt
    now = _dt.datetime.now()
    # 已有剩余时长则叠加
    base = now
    existing = (u.get("admin_until") or "").strip()
    if existing:
        try:
            old = _dt.datetime.strptime(existing, "%Y-%m-%d %H:%M:%S")
            if old > now:
                base = old
        except ValueError:
            pass
    until = (base + _dt.timedelta(days=tier["days"])).strftime("%Y-%m-%d %H:%M:%S")

    with db() as conn:
        conn.execute(
            "UPDATE upload_users SET points=points-?, admin_until=? WHERE username=?",
            (tier["points"], until, username),
        )
    import db as _dbmod
    _dbmod.add_point_log(username, -tier["points"], u["points"] - tier["points"],
                  f"兑换管理员权限 {tier['label']}")
    return {"ok": True, "msg": f"兑换成功！管理员权限至 {until}",
            "spent": tier["points"], "admin_until": until,
            "points": u["points"] - tier["points"]}


def is_timed_admin(username: str) -> bool:
    """是否有时间制管理员权限（含到期检查）。"""
    g = get_points(username)
    return g.get("is_timed_admin", False)


# ---------------- 活动系统 ----------------

def _activities_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
CREATE TABLE IF NOT EXISTS activities (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    title     TEXT NOT NULL,
    body      TEXT NOT NULL DEFAULT '',
    multiplier REAL NOT NULL DEFAULT 1.0,   -- 签到倍率（1=无加成）
    start_at  TEXT NOT NULL DEFAULT '',
    end_at    TEXT NOT NULL DEFAULT '',
    enabled   INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
)""")


def _ensure_activities() -> None:
    with db() as conn:
        _activities_table(conn)


def _ensure_activities_safe() -> None:
    try:
        _ensure_activities()
    except Exception:
        pass


def list_activities(include_disabled: bool = True) -> list[dict]:
    _ensure_activities_safe()
    with db() as conn:
        conn.row_factory = sqlite3.Row
        sql = "SELECT * FROM activities"
        if not include_disabled:
            sql += " WHERE enabled=1"
        rows = conn.execute(sql + " ORDER BY id DESC").fetchall()
    return [dict(r) for r in rows]


def active_activities() -> list[dict]:
    """当前生效中的活动（enabled 且在有效期内），按倍率降序。"""
    import datetime as _dt
    now = _dt.datetime.now()
    out = []
    for a in list_activities(include_disabled=False):
        ok = True
        if a.get("start_at"):
            try:
                if _dt.datetime.strptime(a["start_at"], "%Y-%m-%d %H:%M:%S") > now:
                    ok = False
            except ValueError:
                pass
        if a.get("end_at"):
            try:
                if _dt.datetime.strptime(a["end_at"], "%Y-%m-%d %H:%M:%S") < now:
                    ok = False
            except ValueError:
                pass
        if ok:
            out.append(a)
    out.sort(key=lambda x: -x.get("multiplier", 1.0))
    return out


def current_multiplier() -> float:
    """当前最高生效倍率（默认 1.0）。"""
    acts = active_activities()
    return max([a.get("multiplier", 1.0) for a in acts] or [1.0])


def add_activity(title: str, body: str = "", multiplier: float = 1.0,
                 start_at: str = "", end_at: str = "") -> int | None:
    _ensure_activities_safe()
    with db() as conn:
        cur = conn.execute(
            "INSERT INTO activities(title, body, multiplier, start_at, end_at, enabled) "
            "VALUES(?,?,?,?,?,1)",
            (title.strip(), body.strip(), max(1.0, float(multiplier)),
             start_at.strip(), end_at.strip()),
        )
        return int(cur.lastrowid)


def update_activity(aid: int, title: str, body: str, multiplier: float,
                    start_at: str, end_at: str, enabled: bool) -> None:
    _ensure_activities_safe()
    with db() as conn:
        conn.execute(
            "UPDATE activities SET title=?, body=?, multiplier=?, start_at=?, end_at=?, enabled=? WHERE id=?",
            (title.strip(), body.strip(), max(1.0, float(multiplier)),
             start_at.strip(), end_at.strip(), int(bool(enabled)), aid),
        )


def delete_activity(aid: int) -> None:
    _ensure_activities_safe()
    with db() as conn:
        conn.execute("DELETE FROM activities WHERE id=?", (aid,))


# ---------------- 积分管理（后台） ----------------

def adjust_points(username: str, delta: int, reason: str = "") -> int | None:
    """调整单个账号积分（可为负）。返回新积分，账号不存在返回 None。"""
    u = _get_user(username)
    if not u:
        return None
    with db() as conn:
        conn.execute("UPDATE upload_users SET points=MAX(0, points+?) WHERE username=?",
                     (delta, username))
    new = _get_user(username)
    return new.get("points", 0)


def grant_points_all(delta: int, reason: str = "") -> int:
    """全员加分（可为负，不低于 0）。返回受影响人数。"""
    with db() as conn:
        cur = conn.execute("UPDATE upload_users SET points=MAX(0, points+?) WHERE enabled=1",
                           (delta,))
        return cur.rowcount


def reset_points_all() -> int:
    with db() as conn:
        cur = conn.execute("UPDATE upload_users SET points=0")
        return cur.rowcount

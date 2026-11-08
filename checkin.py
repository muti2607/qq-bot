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

    with db() as conn:
        conn.execute(
            "UPDATE upload_users SET points=points+?, last_checkin=?, streak=? WHERE username=?",
            (gain, today, streak, username),
        )
    u2 = _get_user(username)
    msg = f"签到成功 +{DAILY_POINTS} 分"
    if bonus:
        msg += f"，连续 {streak} 天奖励 +{bonus} 分"
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
    return {"ok": True, "msg": f"兑换成功！管理员权限至 {until}",
            "spent": tier["points"], "admin_until": until,
            "points": u["points"] - tier["points"]}


def is_timed_admin(username: str) -> bool:
    """是否有时间制管理员权限（含到期检查）。"""
    g = get_points(username)
    return g.get("is_timed_admin", False)

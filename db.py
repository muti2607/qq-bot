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

DEFAULT_QUOTES = [
    "语言的隔阂，本质上是一个人——信息，知识，视野，圈层的隔阂。|网络",
    "种一棵树最好的时间是十年前，其次是现在。|谚语",
    "岁月不居，时节如流。|孔融《论盛孝章书》",
    "星光不问赶路人，时光不负有心人。|大冰",
    "凡是过往，皆为序章。|莎士比亚",
    "行到水穷处，坐看云起时。|王维《终南别业》",
    "人生天地之间，若白驹之过隙，忽然而已。|《庄子·知北游》",
    "念念不忘，必有回响。|《一代宗师》",
    "海内存知己，天涯若比邻。|王勃《送杜少府之任蜀州》",
    "万物皆有裂痕，那是光照进来的地方。|莱昂纳德·科恩",
    "你未看此花时，此花与汝心同归于寂。|王阳明《传习录》",
    "关关雎鸠，在河之洲。|《诗经·周南》",
    "路漫漫其修远兮，吾将上下而求索。|屈原《离骚》",
    "长风破浪会有时，直挂云帆济沧海。|李白《行路难》",
    "会当凌绝顶，一览众山小。|杜甫《望岳》",
    "竹杖芒鞋轻胜马，谁怕？一蓑烟雨任平生。|苏轼《定风波》",
    "大漠孤烟直，长河落日圆。|王维《使至塞上》",
    "落霞与孤鹜齐飞，秋水共长天一色。|王勃《滕王阁序》",
    "千淘万漉虽辛苦，吹尽狂沙始到金。|刘禹锡《浪淘沙》",
    "沉舟侧畔千帆过，病树前头万木春。|刘禹锡《酬乐天扬州初逢席上见赠》",
    "山重水复疑无路，柳暗花明又一村。|陆游《游山西村》",
    "纸上得来终觉浅，绝知此事要躬行。|陆游《冬夜读书示子聿》",
    "问渠那得清如许？为有源头活水来。|朱熹《观书有感》",
    "不畏浮云遮望眼，自缘身在最高层。|王安石《登飞来峰》",
    "春风得意马蹄疾，一日看尽长安花。|孟郊《登科后》",
    "天生我材必有用，千金散尽还复来。|李白《将进酒》",
    "安能摧眉折腰事权贵，使我不得开心颜。|李白《梦游天姥吟留别》",
    "捐躯赴国难，视死忽如归。|曹植《白马篇》",
    "老骥伏枥，志在千里。|曹操《龟虽寿》",
    "鞠躬尽瘁，死而后已。|诸葛亮《后出师表》",
    "非淡泊无以明志，非宁静无以致远。|诸葛亮《诫子书》",
    "先天下之忧而忧，后天下之乐而乐。|范仲淹《岳阳楼记》",
    "醉翁之意不在酒，在乎山水之间也。|欧阳修《醉翁亭记》",
    "出淤泥而不染，濯清涟而不妖。|周敦颐《爱莲说》",
    "不以物喜，不以己悲。|范仲淹《岳阳楼记》",
    "天行健，君子以自强不息。|《周易》",
    "地势坤，君子以厚德载物。|《周易》",
    "己所不欲，勿施于人。|《论语·颜渊》",
    "三军可夺帅也，匹夫不可夺志也。|《论语·子罕》",
    "学而不思则罔，思而不学则殆。|《论语·为政》",
    "知之者不如好之者，好之者不如乐之者。|《论语·雍也》",
    "苟日新，日日新，又日新。|《大学》",
    "独学而无友，则孤陋而寡闻。|《礼记·学记》",
    "玉不琢，不成器；人不学，不知道。|《礼记·学记》",
    "千里之行，始于足下。|《道德经》",
    "合抱之木，生于毫末。|《道德经》",
    "上善若水，水善利万物而不争。|《道德经》",
    "知人者智，自知者明。|《道德经》",
    "吾生也有涯，而知也无涯。|《庄子·养生主》",
    "相濡以沫，不如相忘于江湖。|《庄子·大宗师》",
    "鹏程万里，扶摇直上。|《庄子·逍遥游》",
    "亦余心之所善兮，虽九死其犹未悔。|屈原《离骚》",
    "世界是你们的，也是我们的，但是归根结底是你们的。|毛泽东",
    "牢骚太盛防肠断，风物长宜放眼量。|毛泽东《和柳亚子先生》",
    "世上无难事，只要肯登攀。|毛泽东《水调歌头·重上井冈山》",
    "身体是革命的本钱。|毛泽东",
    "黑夜无论怎样悠长，白昼总会到来。|莎士比亚",
    "活着的人总要比死去的人辛苦。|路遥《平凡的世界》",
    "生活不能等待别人来安排，要自己去争取和奋斗。|路遥《平凡的世界》",
    "人间值得，未来可期。|网络",
    "但行好事，莫问前程。|《增广贤文》",
    "博观而约取，厚积而薄发。|苏轼《稼说送张琥》",
    "立志欲坚不欲锐，成功在久不在速。|张孝祥《论治体札子》",
    "古之立大事者，不惟有超世之才，亦必有坚忍不拔之志。|苏轼《晁错论》",
    "功不唐捐，玉汝于成。|张载（西铭）",
    "追风赶月莫停留，平芜尽处是春山。|网络",
    "少年应有鸿鹄志，当骑骏马踏平川。|网络",
    "鲜衣怒马少年时，不负韶华行且知。|网络",
    "道阻且长，行则将至。|《诗经·蒹葭》化用",
    "心心在一艺，其艺必工；心心在一职，其职必举。|《围炉夜话》",
    "运营是设计出来的，不是救火救出来的。|工程师谚语",
    "把每天该说的话准时说出，就是机器人全部的浪漫。|本机",
]


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

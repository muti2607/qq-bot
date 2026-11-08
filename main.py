"""QQ 定时发布机器人 —— Web 管理端 + 调度器。"""
from __future__ import annotations

import asyncio
import base64
import datetime as _dt
import hashlib
import json
import logging
import os
import re
import secrets
import threading
import time
import uuid
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

from apscheduler.triggers.cron import CronTrigger
from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import db
import onebot
import scheduler
import sender
import checkin
import mailer
import triggers

BASE_DIR = Path(__file__).resolve().parent
PORT = int(os.getenv("PORT", "8080"))
REMOTE_PORT = int(os.getenv("REMOTE_PORT", "8001"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("qqbot")

db.init_db()

# 首次启动时可用环境变量预置管理密码（之后在网页“设置”里修改）
if os.getenv("ADMIN_PASSWORD") and not db.get_settings().get("admin_password"):
    db.set_settings({"admin_password": os.getenv("ADMIN_PASSWORD", "")})

# 首次启动时可用环境变量预设 OneBot 地址
if os.getenv("ONEBOT_HTTP") and db.get_settings().get("onebot_http") == db.DEFAULT_SETTINGS["onebot_http"]:
    db.set_settings({"onebot_http": os.getenv("ONEBOT_HTTP", "")})
if os.getenv("ONEBOT_TOKEN"):
    db.set_settings({"onebot_token": os.getenv("ONEBOT_TOKEN", "")})

# OneBot 的阻塞调用统一放到线程里执行，避免卡住事件循环。
async def _onebot_call(fn, *args):
    return await asyncio.to_thread(fn, *args)

app = FastAPI(title="QQ 定时发布机器人", docs_url=None, redoc_url=None)

# ---------------- 登录（单密码 + 签名 Cookie） ----------------

COOKIE_NAME = "qdb_session"


def _fernet() -> Fernet:
    key_file = db.DATA_DIR / "secret.key"
    if not key_file.exists():
        key_file.write_bytes(Fernet.generate_key())
        try:
            key_file.chmod(0o600)
        except OSError:
            pass
    return Fernet(key_file.read_bytes().strip())


def _sign() -> str:
    token = f"{secrets.token_hex(8)}:{int(time.time())}:{_pwd_fingerprint()}"
    return _fernet().encrypt(token.encode()).decode()


def _pwd_fingerprint() -> str:
    pwd = db.get_settings().get("admin_password", "")
    return hashlib.sha256(pwd.encode()).hexdigest()[:12]


def _verify(cookie: str | None) -> bool:
    if not cookie:
        return False
    try:
        raw = _fernet().decrypt(cookie.encode(), ttl=60 * 60 * 24 * 30).decode()
    except (InvalidToken, Exception):
        return False
    # 密码改过之后旧会话立即失效
    parts = raw.split(":")
    return len(parts) == 3 and parts[2] == _pwd_fingerprint()


def _auth_required() -> bool:
    return bool(db.get_settings().get("admin_password"))


async def require_auth(request: Request) -> None:
    if not _auth_required():
        return
    if not _verify(request.cookies.get(COOKIE_NAME)):
        raise HTTPException(status_code=401, detail="未登录")


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path
    public = path.startswith("/login") or path.startswith("/api/login") or path.startswith("/static") or path == "/post"
    if not public and _auth_required() and not _verify(request.cookies.get(COOKIE_NAME)):
        if path.startswith("/api/"):
            return JSONResponse({"detail": "未登录"}, status_code=401)
        return RedirectResponse("/login")
    return await call_next(request)


@app.get("/login", response_class=HTMLResponse)
async def login_page():
    return (BASE_DIR / "static" / "login.html").read_text(encoding="utf-8")


@app.post("/api/login")
async def api_login(request: Request):
    body = await request.json()
    pwd = str(body.get("password") or "")
    if not _auth_required():
        return {"ok": True}
    if not secrets.compare_digest(pwd, db.get_settings().get("admin_password", "")):
        await asyncio.sleep(0.6)
        raise HTTPException(status_code=403, detail="密码错误")
    resp = JSONResponse({"ok": True})
    resp.set_cookie(COOKIE_NAME, _sign(), httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30)
    return resp


@app.post("/api/logout")
async def api_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE_NAME)
    return resp


# ---------------- 页面 ----------------


@app.get("/", response_class=HTMLResponse)
async def index():
    html = (BASE_DIR / "static" / "index.html").read_text(encoding="utf-8")
    # 禁止缓存：改完页面要能立刻生效，否则浏览器会一直用旧的（删除按钮失效就是这个坑）
    return HTMLResponse(html, headers={
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
        "Pragma": "no-cache",
        "Expires": "0",
    })


# ---------------- 设置 / 状态 ----------------


class SettingsIn(BaseModel):
    onebot_http: str | None = None
    onebot_token: str | None = None
    post_token: str | None = None
    remote_token: str | None = None
    smtp_host: str | None = None
    smtp_port: int | None = None
    smtp_user: str | None = None
    smtp_pass: str | None = None
    admin_password: str | None = None
    rss_timeout: int | None = None
    # AI 聊天配置
    ai_enabled: str | None = None
    ai_api_base: str | None = None
    ai_api_key: str | None = None
    ai_model: str | None = None
    ai_system_prompt: str | None = None
    ai_reply_mode: str | None = None
    ai_providers: str | None = None
    ai_vision_providers: str | None = None
    ai_keyless_fallback: str | None = None
    ai_at_reply: str | None = None
    ai_use_face: str | None = None
    ai_use_poke: str | None = None
    ai_use_sticker: str | None = None
    ai_see_image: str | None = None
    ai_save_sticker: str | None = None
    ai_fav_to_qq: str | None = None
    ai_use_qq_fav: str | None = None
    ai_send_animated: str | None = None
    ai_group_gap: str | None = None
    ai_poke_reply_poke: str | None = None


@app.get("/api/settings")
async def api_get_settings():
    s = db.get_settings()
    s["onebot_token"] = "******" if s.get("onebot_token") else ""
    s["post_token"] = "******" if s.get("post_token") else ""
    s["remote_token"] = "******" if s.get("remote_token") else ""
    s["smtp_pass"] = "******" if s.get("smtp_pass") else ""
    s["ai_api_key"] = "******" if s.get("ai_api_key") else ""
    s.pop("admin_password", None)
    s["has_password"] = _auth_required()
    return s


@app.post("/api/settings")
async def api_save_settings(body: SettingsIn):
    data: dict[str, Any] = {}
    if body.onebot_http is not None:
        data["onebot_http"] = body.onebot_http.strip()
    if body.onebot_token is not None and body.onebot_token != "******":
        data["onebot_token"] = body.onebot_token.strip()
    if body.post_token is not None and body.post_token != "******":
        data["post_token"] = body.post_token.strip()
    if body.remote_token is not None and body.remote_token != "******":
        data["remote_token"] = body.remote_token.strip()
    if body.smtp_host is not None:
        data["smtp_host"] = body.smtp_host.strip()
    if body.smtp_port is not None:
        data["smtp_port"] = str(body.smtp_port)
    if body.smtp_user is not None:
        data["smtp_user"] = body.smtp_user.strip()
    if body.smtp_pass is not None and body.smtp_pass != "******":
        data["smtp_pass"] = body.smtp_pass.strip()
    if body.rss_timeout is not None:
        data["rss_timeout"] = str(body.rss_timeout)
    # AI 聊天配置
    if body.ai_enabled is not None:
        data["ai_enabled"] = body.ai_enabled.strip()
    if body.ai_api_base is not None:
        data["ai_api_base"] = body.ai_api_base.strip()
    if body.ai_api_key is not None and body.ai_api_key != "******":
        data["ai_api_key"] = body.ai_api_key.strip()
    if body.ai_model is not None:
        data["ai_model"] = body.ai_model.strip()
    if body.ai_system_prompt is not None:
        data["ai_system_prompt"] = body.ai_system_prompt.strip()
    if body.ai_reply_mode is not None:
        data["ai_reply_mode"] = body.ai_reply_mode.strip()
    if body.ai_providers is not None:
        data["ai_providers"] = body.ai_providers.strip()
    if body.ai_vision_providers is not None:
        data["ai_vision_providers"] = body.ai_vision_providers.strip()
    if body.ai_keyless_fallback is not None:
        data["ai_keyless_fallback"] = body.ai_keyless_fallback.strip()
    for field in ("ai_at_reply", "ai_use_face", "ai_use_poke", "ai_use_sticker",
                  "ai_see_image", "ai_save_sticker", "ai_group_gap", "ai_poke_reply_poke",
                  "ai_fav_to_qq", "ai_send_animated", "ai_use_qq_fav"):
        value = getattr(body, field)
        if value is not None:
            data[field] = value.strip()
    db.set_settings(data)
    return {"ok": True}


@app.post("/api/ai-test")
async def api_ai_test():
    """逐个测试已配置的 AI 接口，返回每个接口是否可用。"""
    import ai

    conf = db.get_settings()
    if conf.get("ai_keyless_fallback") is None:
        db.set_settings({"ai_keyless_fallback": "1"})
    res = await asyncio.to_thread(ai.test)
    if res.get("winner"):
        db.add_log(f"AI 测试：接口「{res['winner']}」可用", "info")
    else:
        db.add_log("AI 测试：所有接口均不可用", "warn")
    return res


class PasswordIn(BaseModel):
    old_password: str = ""
    new_password: str


@app.post("/api/password")
async def api_password(body: PasswordIn):
    cur = db.get_settings().get("admin_password", "")
    if cur and not secrets.compare_digest(body.old_password, cur):
        raise HTTPException(403, "原密码错误")
    new = body.new_password.strip()
    if new and len(new) < 4:
        raise HTTPException(400, "新密码至少 4 位")
    db.set_settings({"admin_password": new})
    return {"ok": True}


@app.get("/api/status")
async def api_status():
    st = await _onebot_call(onebot.get_status)
    st["next_runs"] = scheduler.next_runs()
    st["server_time"] = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return st


# ---------------- 服务器性能仪表盘 ----------------

_last_cpu = {"total": 0, "idle": 0, "ts": 0.0}


def _read_cpu_percent() -> float:
    """与上一次请求采样做差分算 CPU 使用率（前端 1 秒刷新即采样间隔）。"""
    import time as _time

    try:
        with open("/proc/stat") as f:
            parts = [float(x) for x in f.readline().split()[1:]]
        idle = parts[3] + (parts[4] if len(parts) > 4 else 0)
        total = sum(parts)
    except Exception:
        return 0.0
    now = _time.time()
    prev = _last_cpu
    # 首次请求（基准为 0）：只记录，不做差分
    if prev["total"] == 0:
        prev.update(total=total, idle=idle, ts=now, last=0.0)
        return 0.0
    dt_total = total - prev["total"]
    dt_idle = idle - prev["idle"]
    dt_time = now - prev["ts"]
    prev.update(total=total, idle=idle, ts=now)
    # 采样间隔过短（<100ms）时返回上次值，避免抖动
    if dt_total <= 0 or dt_time < 0.1:
        return round(prev.get("last", 0.0), 1)
    val = round(max(0.0, min(100.0, (1 - dt_idle / dt_total) * 100)), 1)
    prev["last"] = val
    return val


def _fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


@app.get("/api/metrics")
async def api_metrics():
    """服务器资源占用：CPU / 内存 / 磁盘 / 负载 / 运行时间 / 容器内存。"""
    import shutil

    out: dict[str, Any] = {}

    # CPU 使用率：与上一次请求采样做差分（首次请求为 0，1 秒后即准确）
    out["cpu_percent"] = _read_cpu_percent()

    try:
        out["cpu_cores"] = os.cpu_count() or 1
    except Exception:
        out["cpu_cores"] = 1

    # CPU 主频：优先 "cpu MHz" 行，其次从 "model name" 里的 @ 2.40GHz 提取
    out["cpu_mhz"] = None
    try:
        model_line = ""
        with open("/proc/cpuinfo") as f:
            for line in f:
                low = line.lower()
                if low.startswith("cpu mhz") or "mhz" in low.split(":")[0]:
                    val = line.split(":")[1].strip().split()[0]
                    out["cpu_mhz"] = round(float(val), 0)
                    break
                if not model_line and low.startswith("model name"):
                    model_line = line
        if out["cpu_mhz"] is None and model_line:
            import re as _re
            m = _re.search(r"@\s*([\d.]+)\s*GHz", model_line, _re.I)
            if m:
                out["cpu_mhz"] = round(float(m.group(1)) * 1000, 0)
    except Exception:
        pass

    # 内存 /proc/meminfo
    try:
        mem = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":")
                mem[k.strip()] = int(v.split()[0]) * 1024
        total = mem.get("MemTotal", 0)
        avail = mem.get("MemAvailable", mem.get("MemFree", 0))
        used = total - avail
        out["mem_total"] = total
        out["mem_used"] = used
        out["mem_percent"] = round(used / total * 100, 1) if total else 0
        out["mem_total_h"] = _fmt_bytes(total)
        out["mem_used_h"] = _fmt_bytes(used)
    except Exception:
        out.update(mem_total=0, mem_used=0, mem_percent=0, mem_total_h="?", mem_used_h="?")

    # swap
    try:
        with open("/proc/meminfo") as f:
            sw = {}
            for line in f:
                k, v = line.split(":")
                sw[k.strip()] = int(v.split()[0]) * 1024
        st, sf = sw.get("SwapTotal", 0), sw.get("SwapFree", 0)
        out["swap_total"] = st
        out["swap_used"] = st - sf
        out["swap_percent"] = round((st - sf) / st * 100, 1) if st else 0
        out["swap_total_h"] = _fmt_bytes(st)
        out["swap_used_h"] = _fmt_bytes(st - sf)
    except Exception:
        out.update(swap_total=0, swap_used=0, swap_percent=0, swap_total_h="?", swap_used_h="?")

    # 磁盘
    try:
        d = shutil.disk_usage("/")
        out["disk_total"] = d.total
        out["disk_used"] = d.used
        out["disk_free"] = d.free
        out["disk_percent"] = round(d.used / d.total * 100, 1)
        out["disk_free_percent"] = round(d.free / d.total * 100, 1)
        out["disk_total_h"] = _fmt_bytes(d.total)
        out["disk_used_h"] = _fmt_bytes(d.used)
        out["disk_free_h"] = _fmt_bytes(d.free)
    except Exception:
        out.update(disk_total=0, disk_used=0, disk_free=0, disk_percent=0,
                   disk_free_percent=0, disk_total_h="?", disk_used_h="?", disk_free_h="?")

    # 负载
    try:
        out["load"] = [round(x, 2) for x in os.getloadavg()]
    except Exception:
        out["load"] = [0, 0, 0]

    # 运行时间
    try:
        with open("/proc/uptime") as f:
            secs = float(f.readline().split()[0])
        d, rem = divmod(int(secs), 86400)
        h, rem = divmod(rem, 3600)
        m = rem // 60
        out["uptime"] = f"{d}天{h}小时{m}分" if d else f"{h}小时{m}分"
    except Exception:
        out["uptime"] = "?"

    # 容器自身内存（cgroup v2 优先，v1 兜底）
    def _cgroup(path: str) -> str | None:
        try:
            return open(path).read().strip()
        except Exception:
            return None

    cur = _cgroup("/sys/fs/cgroup/memory.current")
    mx = _cgroup("/sys/fs/cgroup/memory.max")
    if cur and mx and mx != "max":
        out["container_mem_used"] = int(cur)
        out["container_mem_limit"] = int(mx)
        out["container_mem_percent"] = round(int(cur) / int(mx) * 100, 1)
        out["container_mem_used_h"] = _fmt_bytes(int(cur))
    else:
        c1 = _cgroup("/sys/fs/cgroup/memory/memory.usage_in_bytes")
        l1 = _cgroup("/sys/fs/cgroup/memory/memory.limit_in_bytes")
        if c1 and l1 and int(l1) < 1 << 60:
            out["container_mem_used"] = int(c1)
            out["container_mem_limit"] = int(l1)
            out["container_mem_percent"] = round(int(c1) / int(l1) * 100, 1)
            out["container_mem_used_h"] = _fmt_bytes(int(c1))
        else:
            out["container_mem_used"] = 0
            out["container_mem_percent"] = 0
            out["container_mem_used_h"] = "?"

    # QQ 主进程内存（宿主机 ps，容器内通常看不到宿主机进程，尽力而为）
    out["server_time"] = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return out


# ---------------- 目标列表 ----------------


@app.get("/api/groups")
async def api_groups():
    try:
        groups = await _onebot_call(onebot.get_group_list)
    except onebot.OneBotError as e:
        raise HTTPException(502, str(e))
    out = [
        {
            "type": "group",
            "id": g.get("group_id"),
            "name": g.get("group_name") or str(g.get("group_id")),
            "member_count": g.get("member_count"),
        }
        for g in groups
    ]
    out.sort(key=lambda x: str(x["name"]))
    return out


@app.get("/api/friends")
async def api_friends():
    try:
        friends = await _onebot_call(onebot.get_friend_list)
    except onebot.OneBotError as e:
        raise HTTPException(502, str(e))
    out = [
        {
            "type": "private",
            "id": f.get("user_id"),
            "name": f.get("remark") or f.get("nickname") or str(f.get("user_id")),
        }
        for f in friends
    ]
    out.sort(key=lambda x: str(x["name"]))
    return out


class LookupIn(BaseModel):
    id: int


@app.post("/api/lookup")
async def api_lookup(body: LookupIn):
    """按 QQ 号查昵称（用于手动添加不认识的私聊对象）。"""
    try:
        info = await _onebot_call(onebot.get_stranger_info, body.id)
    except onebot.OneBotError as e:
        raise HTTPException(502, str(e))
    return {"type": "private", "id": body.id, "name": info.get("nickname") or str(body.id)}


# ---------------- 任务 ----------------


class Target(BaseModel):
    type: str
    id: int
    name: str = ""


class TaskVar(BaseModel):
    key: str
    value: str = ""


class TaskIn(BaseModel):
    name: str = ""
    enabled: bool = True
    content: str = ""
    image_id: int | None = None
    image_mode: str = "fixed"          # none | fixed | random
    image_ids: list[int] = Field(default_factory=list)
    vars: list[TaskVar] = Field(default_factory=list)
    rss_url: str = ""
    source_mode: str = "static"
    at_all: bool = False
    targets: list[Target] = Field(default_factory=list)
    schedule: str = "0 9 * * *"
    timezone: str = "Asia/Shanghai"


def _validate(body: TaskIn) -> None:
    try:
        CronTrigger.from_crontab(body.schedule.strip(), timezone=body.timezone or "Asia/Shanghai")
    except Exception as e:
        raise HTTPException(400, f"cron 表达式无效：{e}")
    if body.source_mode not in ("static", "rss_text", "rss_with_link"):
        raise HTTPException(400, "source_mode 无效")
    if body.image_mode not in ("none", "fixed", "random"):
        raise HTTPException(400, "image_mode 无效")
    has_image = (
        (body.image_mode == "fixed" and body.image_id)
        or (body.image_mode == "random")
    )
    if not body.content.strip() and not has_image and body.source_mode == "static":
        raise HTTPException(400, "内容和图片至少填一个")
    if body.source_mode != "static" and not body.rss_url.strip():
        raise HTTPException(400, "RSS 模式需要填写 RSS 地址")
    for t in body.targets:
        if t.type not in ("group", "private"):
            raise HTTPException(400, f"目标类型无效：{t.type}")
    for v in body.vars:
        if not v.key.strip():
            raise HTTPException(400, "自定义变量名不能为空")
        if not re.fullmatch(r"[A-Za-z0-9_\u4e00-\u9fff]+", v.key.strip()):
            raise HTTPException(400, f"变量名「{v.key}」只能包含字母数字下划线或中文")


@app.get("/api/tasks")
async def api_tasks():
    tasks = db.list_tasks()
    nxt = scheduler.next_runs()
    for t in tasks:
        t["next_run"] = nxt.get(t["id"])
        if t.get("image_id"):
            img = db.get_image(int(t["image_id"]))
            t["image"] = img
    return tasks


@app.get("/api/tasks/{task_id}")
async def api_task(task_id: int):
    t = db.get_task(task_id)
    if not t:
        raise HTTPException(404, "任务不存在")
    t["next_run"] = scheduler.next_runs().get(task_id)
    return t


@app.post("/api/tasks")
async def api_create_task(body: TaskIn):
    _validate(body)
    tid = db.create_task(body.model_dump())
    scheduler.reload_task(tid)
    db.add_log(f"新建任务《{body.name or tid}》", "info", tid)
    return {"ok": True, "id": tid}


@app.put("/api/tasks/{task_id}")
async def api_update_task(task_id: int, body: TaskIn):
    if not db.get_task(task_id):
        raise HTTPException(404, "任务不存在")
    _validate(body)
    db.update_task(task_id, body.model_dump())
    scheduler.reload_task(task_id)
    db.add_log(f"修改任务《{body.name or task_id}》", "info", task_id)
    return {"ok": True}


@app.delete("/api/tasks/{task_id}")
async def api_delete_task(task_id: int):
    db.delete_task(task_id)
    scheduler.reload_task(task_id)
    db.add_log(f"删除任务 #{task_id}", "warn", task_id)
    return {"ok": True}


class ToggleIn(BaseModel):
    enabled: bool


@app.post("/api/tasks/{task_id}/toggle")
async def api_toggle_task(task_id: int, body: ToggleIn):
    if not db.get_task(task_id):
        raise HTTPException(404, "任务不存在")
    db.update_task(task_id, {"enabled": body.enabled})
    scheduler.reload_task(task_id)
    db.add_log(f"任务 #{task_id} 已{'启用' if body.enabled else '停用'}", "info", task_id)
    return {"ok": True}


class PreviewIn(BaseModel):
    content: str = ""
    rss_url: str = ""
    source_mode: str = "static"
    name: str = ""
    image_id: int | None = None
    image_mode: str = "fixed"
    image_ids: list[int] = Field(default_factory=list)
    vars: list[TaskVar] = Field(default_factory=list)


@app.post("/api/preview")
async def api_preview(body: PreviewIn):
    """预览渲染后的文本（不发送）。"""
    task = {
        "content": body.content,
        "rss_url": body.rss_url,
        "source_mode": body.source_mode,
        "name": body.name,
        "vars": [v.model_dump() for v in body.vars],
        "image_mode": "none",  # 预览只展示文本
        "image_id": None,
        "image_ids": [],
    }
    now = _dt.datetime.now(_tz())
    try:
        segments = await _onebot_call(sender.build_segments, task, now)
    except Exception as e:
        raise HTTPException(400, str(e))
    parts = []
    for seg in segments:
        if seg["type"] == "text":
            parts.append(seg["data"]["text"])
        elif seg["type"] == "image":
            parts.append("[图片]")
    return {"text": "\n".join(parts)}


class RunIn(BaseModel):
    text: str | None = None
    target_type: str | None = None  # 仅测试发送时使用
    target_id: int | None = None


@app.post("/api/tasks/{task_id}/run")
async def api_run_task(task_id: int, body: RunIn | None = None):
    if not db.get_task(task_id):
        raise HTTPException(404, "任务不存在")
    override_targets = None
    if body and body.target_type and body.target_id:
        override_targets = [{"type": body.target_type, "id": body.target_id, "name": "测试目标"}]
    res = await _onebot_call(
        scheduler.run_task, task_id, (body.text if body else None), override_targets
    )
    db.add_log(f"手动执行任务 #{task_id}：成功 {res['sent']}，失败 {res['failed']}", "info", task_id)
    if not res["ok"]:
        raise HTTPException(400, "；".join(res["errors"]) or "发送失败")
    return res


# ---------------- OneBot 事件回调（NapCat HTTP POST 推送） ----------------


@app.post("/post")
async def onebot_event(request: Request):
    """NapCat 的 HTTP POST 上报入口。带 X-Self-Info 或自配 Token 校验。"""
    raw = await request.body()
    # 原始请求落盘：排查“消息没反应”时唯一可信的证据
    try:
        with open("/tmp/post_raw.log", "a", encoding="utf-8") as f:
            hdr = {k: v for k, v in request.headers.items()
                   if k.lower() in ("authorization", "x-self-info", "content-type", "user-agent")}
            f.write(f"{time.strftime('%H:%M:%S')} HEADERS={hdr} BODY={raw[:1200].decode('utf-8', 'replace')}\n")
    except Exception:
        pass
    expected = db.get_settings().get("post_token") or ""
    if expected:
        # NapCat 的 HTTP 客户端推送不带 Authorization（实测 headers 只有 content-type），
        # 所以 token 只作为「公网门禁」：来自 docker 内网的请求放行，
        # 从公网打进来的必须带对 token，防止伪造事件让机器人乱发消息。
        client_ip = (request.client.host if request.client else "") or ""
        from_lan = (client_ip.startswith("172.") or client_ip.startswith("10.")
                    or client_ip.startswith("127.") or client_ip.startswith("192.168."))
        got = request.headers.get("Authorization", "").strip()
        if got.lower().startswith("bearer "):
            got = got[7:].strip()
        if not from_lan and got != expected:
            db.add_log(
                f"公网事件被拒（来源={client_ip}，Token 不匹配）",
                "warn", None)
            return {"ok": False}
    try:
        event = json.loads(raw)
    except Exception as e:
        db.add_log(f"事件上报解析失败：{e}", "warn", None)
        return {"ok": False}
    ptype = event.get("post_type")
    if ptype == "message":
        mtype = event.get("message_type") or ("group" if event.get("group_id") else "private")
        db.add_log(
            f"收到 {mtype} 消息（{event.get('group_id') or event.get('user_id')}）："
            + str(event.get("raw_message") or event.get("message"))[:100],
            "info", None)
        triggers.handle_event_async(event)
    elif ptype == "notice" and event.get("sub_type") == "poke":
        db.add_log(f"被戳了一下（来自 {event.get('user_id')}）", "info", None)
        triggers.handle_poke_async(event)
    return {"ok": True}


# ---------------- 群组预设 ----------------


class PresetIn(BaseModel):
    name: str
    targets: list[Target] = Field(default_factory=list)


@app.get("/api/presets")
async def api_presets():
    return db.list_presets()


@app.post("/api/presets")
async def api_create_preset(body: PresetIn):
    if not body.name.strip():
        raise HTTPException(400, "预设名称不能为空")
    for t in body.targets:
        if t.type not in ("group", "private"):
            raise HTTPException(400, f"目标类型无效：{t.type}")
    pid = db.create_preset(body.name, [t.model_dump() for t in body.targets])
    db.add_log(f"新建群组预设《{body.name}》（{len(body.targets)} 个目标）", "info")
    return {"ok": True, "id": pid}


@app.put("/api/presets/{pid}")
async def api_update_preset(pid: int, body: PresetIn):
    if not any(p["id"] == pid for p in db.list_presets()):
        raise HTTPException(404, "预设不存在")
    if not body.name.strip():
        raise HTTPException(400, "预设名称不能为空")
    db.update_preset(pid, body.name, [t.model_dump() for t in body.targets])
    return {"ok": True}


@app.delete("/api/presets/{pid}")
async def api_delete_preset(pid: int):
    db.delete_preset(pid)
    db.add_log(f"删除群组预设 #{pid}", "warn")
    return {"ok": True}


# ---------------- 触发词 ----------------


class TriggerIn(BaseModel):
    keyword: str
    reply_text: str = ""
    image_mode: str = "none"
    image_id: int | None = None
    image_ids: list[int] = Field(default_factory=list)
    at_required: bool = True
    at_reply: bool = True      # 回复时是否 @ 触发者
    scope: str = "all"
    cooldown: int = 10
    quote_mode: str = "off"   # off | replace | append
    enabled: bool = True


def _validate_trigger(body: TriggerIn) -> None:
    if not body.keyword.strip():
        raise HTTPException(400, "触发词不能为空")
    if body.image_mode not in ("none", "fixed", "random"):
        raise HTTPException(400, "image_mode 无效")
    if body.quote_mode not in ("off", "replace", "append"):
        raise HTTPException(400, "quote_mode 无效")
    if not body.reply_text.strip() and body.image_mode == "none" and body.quote_mode == "off":
        raise HTTPException(400, "回复内容、图片和随机名言至少选一个")
    if body.scope not in ("all", "group", "private") and not body.scope.startswith("group:"):
        raise HTTPException(400, "scope 无效")


@app.get("/api/triggers")
async def api_triggers():
    return db.list_triggers()


@app.post("/api/triggers")
async def api_create_trigger(body: TriggerIn):
    _validate_trigger(body)
    tid = db.create_trigger(body.model_dump())
    db.add_log(f"新建触发词《{body.keyword}》", "info")
    return {"ok": True, "id": tid}


@app.put("/api/triggers/{tid}")
async def api_update_trigger(tid: int, body: TriggerIn):
    if not db.get_trigger(tid):
        raise HTTPException(404, "触发词不存在")
    _validate_trigger(body)
    db.update_trigger(tid, body.model_dump())
    db.add_log(f"修改触发词《{body.keyword}》", "info")
    return {"ok": True}


@app.delete("/api/triggers/{tid}")
async def api_delete_trigger(tid: int):
    db.delete_trigger(tid)
    db.add_log(f"删除触发词 #{tid}", "warn")
    return {"ok": True}


# ---------------- 名言池 ----------------


class QuotesIn(BaseModel):
    quotes: list[str]


@app.get("/api/quotes")
async def api_quotes():
    custom = bool((db.get_settings().get("quotes") or "").strip())
    return {"custom": custom, "quotes": db.get_quotes(), "default_count": len(db.DEFAULT_QUOTES)}


@app.post("/api/quotes")
async def api_save_quotes(body: QuotesIn):
    items = [q.strip() for q in body.quotes if q.strip()]
    if not items:
        raise HTTPException(400, "名言列表不能为空（想恢复默认请点重置）")
    if len(items) > 10000:
        raise HTTPException(400, "最多 10000 条")
    db.set_settings({"quotes": json.dumps(items, ensure_ascii=False)})
    db.add_log(f"更新自定义名言池（{len(items)} 条）", "info")
    return {"ok": True, "count": len(items)}


@app.delete("/api/quotes")
async def api_reset_quotes():
    db.set_settings({"quotes": ""})
    db.add_log("名言池已恢复默认", "info")
    return {"ok": True}


# ---------------- 图片库 ----------------


# ---------------- 表情库（机器人自己偷的表情） ----------------


class StickerNoteIn(BaseModel):
    note: str | None = None
    tags: str | None = None
    usage: str | None = None


class ClientErrorIn(BaseModel):
    where: str = ""
    id: int | None = None
    message: str = ""


@app.post("/api/client-error")
async def api_client_error(body: ClientErrorIn):
    """前端把报错上报到这里，方便在「运行日志」里看到浏览器侧到底出了什么问题。"""
    db.add_log(
        f"前端报错[{body.where} id={body.id}]：{body.message[:300]}",
        "error", None)
    return {"ok": True}


@app.get("/api/stickers")
async def api_stickers():
    rows = db.list_stickers(limit=500)
    return [{
        "id": r["id"],
        "note": r.get("note") or "",
        "tags": r.get("tags") or "",
        "usage": r.get("usage") or "",
        "used_count": r.get("used_count") or 0,
        "created_at": r.get("created_at") or "",
    } for r in rows]


@app.get("/api/stickers/{sid}/data")
async def api_sticker_data(sid: int):
    row = db.get_sticker(sid)
    if not row:
        raise HTTPException(404, "表情不存在")
    try:
        path = Path(db.sticker_path(row))
        return Response(content=path.read_bytes(), media_type="image/png")
    except OSError:
        raise HTTPException(404, "表情文件丢失")


@app.post("/api/stickers/{sid}")
async def api_sticker_note(sid: int, body: StickerNoteIn):
    if not db.get_sticker(sid):
        raise HTTPException(404, "表情不存在")
    db.set_sticker_note(sid, note=body.note, tags=body.tags, usage=body.usage)
    return {"ok": True}


@app.delete("/api/stickers/{sid}")
async def api_sticker_delete(sid: int):
    db.delete_sticker(sid)
    db.add_log(f"删除表情 #{sid}", "warn")
    return {"ok": True}


@app.post("/api/stickers/{sid}/favorite")
async def api_sticker_favorite(sid: int):
    """把这张表情收藏进 QQ 的「收藏表情」。"""
    import stickers as st
    row = db.get_sticker(sid)
    if not row:
        raise HTTPException(404, "表情不存在")
    ok = await asyncio.to_thread(st.favorite_to_qq, row)
    if not ok:
        raise HTTPException(400, "收藏失败，检查 NapCat 是否在线")
    db.add_log(f"手动收藏表情 #{sid} 到 QQ", "info")
    return {"ok": True}


@app.get("/api/qq-favorites")
async def api_qq_favorites():
    """QQ 收藏表情列表（只给数量，URL 太长）。"""
    import stickers as st
    urls = await asyncio.to_thread(st.qq_favorites)
    return {"count": len(urls), "sample": urls[:3]}


class TestStickerIn(BaseModel):
    target_type: str = "private"
    target_id: int = 0


@app.post("/api/test-qq-favorite")
async def api_test_qq_favorite(body: TestStickerIn):
    """测试：从 QQ 收藏表情里挑一张发到指定目标（默认发给自己）。"""
    import stickers as st
    tid = body.target_id
    if not tid:
        import onebot as ob
        try:
            me = ob.get_login_info() or {}
            tid = int(me.get("user_id") or 0)
        except Exception:
            raise HTTPException(400, "拿不到机器人自己的 QQ 号")
    if not tid:
        raise HTTPException(400, "请填写目标")

    ok = await asyncio.to_thread(st.send_qq_favorite, body.target_type, tid)
    if not ok:
        raise HTTPException(400, "发送失败：QQ 收藏表情可能是空的，或 NapCat 不可用")
    db.add_log(f"测试发送 QQ 收藏表情到 {body.target_type} {tid}", "info")
    return {"ok": True, "count": len(st._fav_urls())}


# ---------------- 会话上下文 ----------------


@app.get("/api/memory")
async def api_memory(session: str | None = None, limit: int = 30):
    """查看某个会话的上下文；不传 session 则列出所有会话概况。"""
    import ai as ai_mod
    if session:
        msgs = ai_mod._history_get(session)
        return {"session": session, "count": len(msgs), "messages": msgs[-limit:]}
    with ai_mod._HISTORY_LOCK:
        items = [{"session": k, "count": len(v["msgs"]), "at": v["at"]}
                 for k, v in ai_mod._HISTORY.items()]
    items.sort(key=lambda x: -x["at"])
    return {"sessions": items[:50], "total": len(items)}


@app.delete("/api/memory")
async def api_memory_clear(session: str | None = None):
    """清空某会话上下文；不传 session 清空全部。"""
    import ai as ai_mod
    if session:
        ai_mod.reset_history(session)
        db.add_log(f"清空会话上下文：{session}", "info")
    else:
        with ai_mod._HISTORY_LOCK:
            keys = list(ai_mod._HISTORY.keys())
            ai_mod._HISTORY.clear()
        for k in keys:
            try:
                db.drop_memory(k)
            except Exception:
                pass
        db.add_log(f"清空全部会话上下文（{len(keys)} 个）", "info")
    return {"ok": True}


@app.get("/api/images")
async def api_images(status: str | None = None):
    """后台图片库：默认返回全部（含待审核），可 ?status=pending 只看待审。"""
    return db.list_images(status=status)


# ---------------- 图片审核 ----------------


class ReviewIn(BaseModel):
    ids: list[int]
    status: str          # approved | rejected | pending
    note: str = ""


@app.get("/api/images/pending")
async def api_pending_images():
    return db.list_images(status="pending")


@app.post("/api/images/review")
async def api_review_images(body: ReviewIn):
    if body.status not in ("approved", "rejected", "pending"):
        raise HTTPException(400, "status 无效")
    if not body.ids:
        raise HTTPException(400, "未选择图片")
    n = db.bulk_review(body.ids, body.status, body.note[:200])
    db.add_log(f"图片审核：{n} 张 → {body.status}" + (f"（{body.note[:30]}）" if body.note else ""), "info")
    return {"ok": True, "count": n}


# ---------------- 远程上传账号（后台管理） ----------------


class UploadUserIn(BaseModel):
    username: str
    password: str = ""
    note: str = ""
    enabled: bool = True
    is_admin: bool = False


@app.get("/api/upload-users")
async def api_upload_users():
    return db.list_upload_users()


@app.post("/api/upload-users")
async def api_create_upload_user(body: UploadUserIn):
    if not body.username.strip() or not body.password:
        raise HTTPException(400, "账号和密码不能为空")
    if len(body.password) < 4:
        raise HTTPException(400, "密码至少 4 位")
    uid = db.create_upload_user(body.username, body.password, body.note, body.is_admin)
    if uid is None:
        raise HTTPException(400, "账号已存在")
    role = "管理员" if body.is_admin else "普通"
    db.add_log(f"创建上传账号 {body.username}（{role}）", "info")
    return {"ok": True, "id": uid}


@app.put("/api/upload-users/{uid}")
async def api_update_upload_user(uid: int, body: UploadUserIn):
    if body.password and len(body.password) < 4:
        raise HTTPException(400, "密码至少 4 位")
    # 账号名可改：传入不同名字时先改名（重名校验）
    cur = db.get_upload_user_by_id(uid)
    if not cur:
        raise HTTPException(404, "账号不存在")
    if body.username.strip() and body.username.strip() != cur["username"]:
        if not re.fullmatch(r"[A-Za-z0-9_\u4e00-\u9fff]{2,20}", body.username.strip()):
            raise HTTPException(400, "账号名需 2-20 位字母/数字/下划线/中文")
        if not db.rename_upload_user(uid, body.username.strip()):
            raise HTTPException(400, "新账号名已被使用")
    db.update_upload_user(uid, password=body.password or None,
                          note=body.note, enabled=body.enabled, is_admin=body.is_admin)
    db.add_log(f"修改上传账号 {cur['username']}" +
               ("（改名 → " + body.username.strip() + "）" if body.username.strip() != cur["username"] else ""), "info")
    return {"ok": True}


@app.get("/api/apply-requests")
async def api_apply_requests(status: str | None = None):
    return db.list_applies(status)


@app.post("/api/apply-requests/{aid}/approve")
async def api_approve_apply(aid: int):
    uid = db.approve_apply(aid)
    if uid is None:
        raise HTTPException(400, "申请不存在、已处理或用户名冲突")
    a = db.get_apply(aid)
    db.add_log(f"注册申请已批准：{a['username']}（{a['mail']}）", "info")
    return {"ok": True, "uid": uid}


@app.post("/api/apply-requests/{aid}/reject")
async def api_reject_apply(aid: int):
    a = db.get_apply(aid)
    if not a or a["status"] != "pending":
        raise HTTPException(400, "申请不存在或已处理")
    db.set_apply_status(aid, "rejected")
    db.add_log(f"注册申请已驳回：{a['username']}", "warn")
    return {"ok": True}


class BanIn(BaseModel):
    hours: int = 0           # 封禁小时数，0 且 permanent=False 表示解封
    permanent: bool = False
    reason: str = ""


@app.post("/api/upload-users/{uid}/ban")
async def api_ban_user(uid: int, body: BanIn):
    """封禁账号：hours 小时 / permanent 永久 / 都不传则解封。"""
    u = db.get_upload_user_by_id(uid)
    if not u:
        raise HTTPException(404, "账号不存在")
    if body.permanent:
        db.ban_user(uid, "permanent", body.reason)
        db.add_log(f"封禁账号 {u['username']}（永久）" +
                   (f"：{body.reason[:40]}" if body.reason else ""), "warn")
        return {"ok": True, "banned": True, "until": "永久"}
    if body.hours <= 0:
        db.unban_user(uid)
        db.add_log(f"解封账号 {u['username']}", "info")
        return {"ok": True, "banned": False}
    import datetime as _dt
    until = (_dt.datetime.now() + _dt.timedelta(hours=body.hours)).strftime("%Y-%m-%d %H:%M:%S")
    db.ban_user(uid, until, body.reason)
    db.add_log(f"封禁账号 {u['username']} 至 {until}" +
               (f"：{body.reason[:40]}" if body.reason else ""), "warn")
    return {"ok": True, "banned": True, "until": until}


# ---------------- 积分活动 / 积分管理（后台） ----------------


class ActivityIn(BaseModel):
    title: str
    body: str = ""
    multiplier: float = 1.0
    start_at: str = ""
    end_at: str = ""
    enabled: bool = True


def _acts():
    import checkin
    checkin._ensure_activities_safe()
    return checkin


@app.get("/api/activities")
async def api_activities_v2():
    return _acts().list_activities()


@app.post("/api/activities")
async def api_add_activity(body: ActivityIn):
    acts = _acts()
    if not body.title.strip():
        raise HTTPException(400, "活动标题不能为空")
    aid = acts.add_activity(body.title, body.body, body.multiplier,
                            body.start_at, body.end_at)
    db.add_log(f"发布积分活动「{body.title}」（倍率 {body.multiplier:g}）", "info")
    return {"ok": True, "id": aid}


@app.put("/api/activities/{aid}")
async def api_update_activity(aid: int, body: ActivityIn):
    acts = _acts()
    acts.update_activity(aid, body.title, body.body, body.multiplier,
                         body.start_at, body.end_at, body.enabled)
    return {"ok": True}


@app.delete("/api/activities/{aid}")
async def api_delete_activity(aid: int):
    _acts().delete_activity(aid)
    return {"ok": True}


class PointsAdjustIn(BaseModel):
    username: str = ""       # 空 = 全员
    delta: int = 0
    reason: str = ""
    reset_all: bool = False


@app.post("/api/points/adjust")
async def api_points_adjust(body: PointsAdjustIn):
    acts = _acts()
    if body.reset_all:
        n = acts.reset_points_all()
        db.add_log(f"全员积分清零（{n} 人）", "warn")
        return {"ok": True, "affected": n}
    if body.username:
        new = acts.adjust_points(body.username.strip(), body.delta, body.reason)
        if new is None:
            raise HTTPException(404, "账号不存在")
        db.add_log(f"调整 {body.username} 积分 {body.delta:+d} → {new}" +
                   (f"（{body.reason[:30]}）" if body.reason else ""), "info")
        return {"ok": True, "username": body.username, "points": new}
    # 全员
    n = acts.grant_points_all(body.delta, body.reason)
    db.add_log(f"全员积分 {body.delta:+d}（{n} 人受影响）" +
               (f"：{body.reason[:30]}" if body.reason else ""), "info")
    return {"ok": True, "affected": n}


@app.delete("/api/upload-users/{uid}")
async def api_delete_upload_user(uid: int):
    db.delete_upload_user(uid)
    return {"ok": True}


@app.post("/api/images")
async def api_upload_image(file: UploadFile = File(...)):
    ext = Path(file.filename or "").suffix.lower() or ".png"
    if ext not in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"):
        raise HTTPException(400, "仅支持 png/jpg/jpeg/gif/webp/bmp")
    name = Path(file.filename or "").stem[:20].replace("/", "_") or "img"
    name = f"{name}-{uuid.uuid4().hex}{ext}"
    dest = db.UPLOAD_DIR / name
    size = 0
    try:
        with dest.open("wb") as f:
            while chunk := await file.read(1 << 20):
                size += len(chunk)
                if size > 10 * 1024 * 1024:
                    raise HTTPException(400, "图片不能超过 10MB")
                f.write(chunk)
    except HTTPException:
        dest.unlink(missing_ok=True)
        raise
    h = hashlib.sha256(dest.read_bytes()).hexdigest()
    dup = db.find_image_by_hash(h)
    if dup:
        dest.unlink(missing_ok=True)
        raise HTTPException(400, f"图片已存在（#{dup['id']} {dup['original']}，上传者 {dup['uploader'] or '后台'}），请勿重复上传")
    iid = db.add_image(name, file.filename or name, size, status="approved", hash=h)
    db.add_log(f"上传图片 {file.filename}（{size // 1024} KB）", "info")
    return {"ok": True, "id": iid}


@app.get("/api/images/{image_id}/file")
async def api_image_file(image_id: int):
    img = db.get_image(image_id)
    if not img:
        raise HTTPException(404, "图片不存在")
    path = db.UPLOAD_DIR / img["filename"]
    if not path.exists():
        raise HTTPException(404, "文件丢失")
    resp = Response(path.read_bytes(), media_type=_guess_mime(img["filename"]))
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"  # 文件名含 uuid，永久缓存
    return resp


THUMB_DIR = db.DATA_DIR / "thumbs"


def _ensure_thumb(img: dict) -> Path | None:
    """生成/复用 200px 缩略图（WebP，体积约为原图的 1/10）。失败返回 None。"""
    try:
        from PIL import Image, ImageOps

        THUMB_DIR.mkdir(parents=True, exist_ok=True)
        src = db.UPLOAD_DIR / img["filename"]
        dst = THUMB_DIR / (Path(img["filename"]).stem + ".webp")
        if dst.exists():
            return dst
        with Image.open(src) as im:
            im = ImageOps.exif_transpose(im)
            im.thumbnail((200, 200), Image.LANCZOS)
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            im.save(dst, "WEBP", quality=72, method=4)
        return dst
    except Exception as e:
        log.warning("缩略图生成失败 %s: %s", img.get("filename"), e)
        return None


@app.get("/api/images/{image_id}/thumb")
async def api_image_thumb(image_id: int):
    """缩略图：后台网格用这个，避免加载原图。"""
    img = db.get_image(image_id)
    if not img:
        raise HTTPException(404, "图片不存在")
    src = db.UPLOAD_DIR / img["filename"]
    if not src.exists():
        raise HTTPException(404, "文件丢失")
    thumb = _ensure_thumb(img)
    if thumb and thumb.exists():
        resp = Response(thumb.read_bytes(), media_type="image/webp")
    else:
        resp = Response(src.read_bytes(), media_type=_guess_mime(img["filename"]))
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


def _data_uri(filename: str) -> str:
    path = db.UPLOAD_DIR / filename
    if not path.exists():
        raise HTTPException(404, "文件丢失")
    b64 = base64.b64encode(path.read_bytes()).decode()
    return f"data:{_guess_mime(filename)};base64,{b64}"


@app.get("/api/images/{image_id}/data")
async def api_image_data(image_id: int):
    """返回 data URI，方便页面直接显示（某些浏览器无法加载无扩展名的图片路径）。"""
    img = db.get_image(image_id)
    if not img:
        raise HTTPException(404, "图片不存在")
    return {"data": _data_uri(img["filename"])}


@app.delete("/api/images/{image_id}")
async def api_delete_image(image_id: int):
    img = db.get_image(image_id)
    if not img:
        raise HTTPException(404, "图片不存在")
    db.delete_image(image_id)
    (db.UPLOAD_DIR / img["filename"]).unlink(missing_ok=True)
    (THUMB_DIR / (Path(img["filename"]).stem + ".webp")).unlink(missing_ok=True)
    return {"ok": True}


class BatchDeleteIn(BaseModel):
    ids: list[int] = Field(default_factory=list)
    all: bool = False          # true = 清空图片库


@app.post("/api/images/batch-delete")
async def api_batch_delete_images(body: BatchDeleteIn):
    """批量删除图片。all=true 时清空整个图片库。"""
    if body.all:
        targets = [i["id"] for i in db.list_images()]
    else:
        if not body.ids:
            raise HTTPException(400, "请选择要删除的图片")
        if len(body.ids) > 2000:
            raise HTTPException(400, "一次最多删除 2000 张")
        targets = body.ids

    deleted = 0
    missing = 0
    for iid in targets:
        img = db.get_image(int(iid))
        if not img:
            missing += 1
            continue
        db.delete_image(int(iid))
        (db.UPLOAD_DIR / img["filename"]).unlink(missing_ok=True)
        (THUMB_DIR / (Path(img["filename"]).stem + ".webp")).unlink(missing_ok=True)
        deleted += 1

    db.add_log(f"批量删除图片 {deleted} 张" + (f"（{missing} 张已不存在）" if missing else ""), "warn")
    return {"ok": True, "deleted": deleted, "missing": missing}


def _guess_mime(name: str) -> str:
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
    }.get(Path(name).suffix.lower(), "application/octet-stream")


# ---------------- 远程上传 API（防 DoS） ----------------

remote_router = APIRouter()

# 远程上传登录态：签名 token（含账号+过期时间）
_UPLOAD_SESSION_HOURS = 24 * 7

def _make_upload_token(username: str) -> str:
    payload = f"{username}:{int(time.time()) + _UPLOAD_SESSION_HOURS * 3600}"
    return _fernet().encrypt(payload.encode()).decode()

def _parse_upload_token(tok: str) -> str | None:
    try:
        raw = _fernet().decrypt(tok.encode(), ttl=_UPLOAD_SESSION_HOURS * 3600).decode()
        username, exp = raw.rsplit(":", 1)
        if int(exp) < time.time():
            return None
        u = db.get_upload_user(username)
        if not u or not u["enabled"]:
            return None
        return username
    except Exception:
        return None

def _require_admin_ctx(request: Request, admin_password: str = "") -> str:
    """8001 审批鉴权：管理员账号登录态 OR 正确管理密码，任一通过即可。
    返回操作者标识（用于日志）。管理员账号的操作不计入限流。"""
    # 方式一：上传账号登录且该账号是管理员
    tok = (request.headers.get("X-Upload-Session", "").strip()
           or request.cookies.get("upload_session", ""))
    who = _parse_upload_token(tok) if tok else None
    if who:
        rec = db.get_upload_user(who)
        if rec and rec.get("enabled"):
            timed = checkin.is_timed_admin(who)
            if rec.get("is_admin") or timed:
                # 管理员登录态：豁免该 IP 的限流（清计数与封禁）
                ip = _client_ip(request)
                with _RATE_LOCK:
                    _RATE_WINDOWS.pop(ip, None)
                    _BAN_UNTIL.pop(ip, None)
                    _BAN_UNTIL.pop(ip + ":hits", None)
                tag = "管理员账号" if rec.get("is_admin") else f"积分管理员(至 {rec.get('admin_until', '')[:10]})"
                return f"{who}({tag})"
    # 方式二：管理密码
    cur = db.get_settings().get("admin_password", "")
    if cur and admin_password and secrets.compare_digest(admin_password, cur):
        return "管理密码"
    raise HTTPException(401, "需要管理员权限：请用管理员账号登录，或输入管理密码")


def _upload_user_from(request: Request) -> str:
    """从 Header / Cookie 取出上传账号，失败抛 401。"""
    tok = (request.headers.get("X-Upload-Session", "").strip()
           or request.cookies.get("upload_session", ""))
    u = _parse_upload_token(tok) if tok else None
    if not u:
        raise HTTPException(401, "请先登录上传账号")
    return u


# 滑动窗口限流：每 IP 在窗口期内最多 N 次请求
_RATE_LOCK = threading.Lock()
_RATE_WINDOWS: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=64))
_RATE_LIMIT = 60          # 每窗口期最多请求次数（放宽：面板一次要拉 3 个接口）
_RATE_WINDOW = 60.0       # 窗口期秒数
_BAN_UNTIL: dict[str, float] = {}          # ip -> 解封时间戳
_BAN_THRESHOLD = 3        # 窗口期内超限次数达到该值即封禁
_BAN_SECONDS = 600        # 封禁时长（10 分钟）
# 上传体积限制（防止超大文件打满磁盘/带宽）
_REMOTE_MAX_FILE_MB = 10
_REMOTE_MAX_BODY_MB = 12


def _client_ip(request: Request) -> str:
    """取真实客户端 IP（若有反代 X-Forwarded-For 则取第一个）。"""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip() or (request.client.host if request.client else "?")
    return request.client.host if request.client else "?"


def _rate_check(ip: str) -> tuple[bool, str]:
    """返回 (是否放行, 拒绝原因)。"""
    now = time.time()
    with _RATE_LOCK:
        # 封禁检查
        ban_until = _BAN_UNTIL.get(ip, 0)
        if ban_until > now:
            return False, f"请求过于频繁，已临时封禁至 {_dt.datetime.fromtimestamp(ban_until).strftime('%H:%M:%S')}"
        # 清理过期封禁（惰性）
        if _BAN_UNTIL and len(_BAN_UNTIL) > 1000:
            for k in [k for k, v in _BAN_UNTIL.items() if v <= now]:
                _BAN_UNTIL.pop(k, None)
        win = _RATE_WINDOWS[ip]
        win.append(now)
        # 只留窗口期内的
        while win and now - win[0] > _RATE_WINDOW:
            win.popleft()
        if len(win) > _RATE_LIMIT:
            hits = _BAN_UNTIL.get(ip + ":hits", 0) + 1
            if hits >= _BAN_THRESHOLD:
                _BAN_UNTIL[ip] = now + _BAN_SECONDS
                _BAN_UNTIL.pop(ip + ":hits", None)
                _RATE_WINDOWS.pop(ip, None)  # 封禁期间不再计数
                db.add_log(f"远程上传：IP {ip} 触发限流被临时封禁 {_BAN_SECONDS}s", "warn")
                return False, "请求过于频繁，已临时封禁 10 分钟"
            _BAN_UNTIL[ip + ":hits"] = hits
            return False, f"请求过于频繁（每 {_RATE_WINDOW:.0f}s 最多 {_RATE_LIMIT} 次），请稍后再试"
        return True, ""


async def _guard_remote_upload(request: Request) -> str:
    """远程上传的公共防护：限流 + Token 鉴权 + 体积检查。返回客户端 ip。"""
    ip = _client_ip(request)
    ok, reason = _rate_check(ip)
    if not ok:
        raise HTTPException(429, reason)
    expected = db.get_settings().get("remote_token") or ""
    if expected:
        got = (request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
               or request.headers.get("X-Upload-Token", "").strip())
        if not secrets.compare_digest(got, expected):
            raise HTTPException(401, "上传 Token 无效")
    length = request.headers.get("content-length")
    if length and int(length) > _REMOTE_MAX_BODY_MB * 1024 * 1024:
        raise HTTPException(413, f"请求体超过 {_REMOTE_MAX_BODY_MB}MB 限制")
    return ip


class RemoteUploadIn(BaseModel):
    """base64 上传（适合脚本/手机快捷指令推送）。"""
    filename: str = ""
    data: str  # base64，不带 data: 前缀也行


@remote_router.post("/remote/upload")
async def remote_upload_json(request: Request, body: RemoteUploadIn):
    """远程 JSON/base64 上传（需登录账号，图片进入待审核）。"""
    ip = await _guard_remote_upload(request)
    uploader = _upload_user_from(request)
    raw_b64 = body.data.strip()
    if raw_b64.startswith("data:"):
        raw_b64 = raw_b64.split(",", 1)[-1]
    if not raw_b64:
        raise HTTPException(400, "data 为空")
    if len(raw_b64) > _REMOTE_MAX_FILE_MB * 1024 * 1024 * 4 / 3:
        raise HTTPException(413, f"文件超过 {_REMOTE_MAX_FILE_MB}MB 限制")
    try:
        raw = base64.b64decode(raw_b64, validate=True)
    except Exception:
        raise HTTPException(400, "data 不是有效的 base64")
    if len(raw) > _REMOTE_MAX_FILE_MB * 1024 * 1024:
        raise HTTPException(413, f"文件超过 {_REMOTE_MAX_FILE_MB}MB 限制")
    name = Path(body.filename or "remote.png").name[:40].replace("/", "_") or "remote"
    ext = Path(name).suffix.lower()
    if ext not in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"):
        ext = ".png"
    fname = f"{name.rsplit(ext, 1)[0][:20]}-{uuid.uuid4().hex}{ext}"
    dest = db.UPLOAD_DIR / fname
    dest.write_bytes(raw)
    h = hashlib.sha256(raw).hexdigest()
    dup = db.find_image_by_hash(h)
    if dup:
        dest.unlink(missing_ok=True)
        raise HTTPException(409, f"图片重复：图库中已存在相同内容的图片（#{dup['id']}），已被自动拒绝")
    iid = db.add_image(fname, name, len(raw), status="pending", uploader=uploader, hash=h)
    db.add_log(f"待审核图片 {name}（{len(raw) // 1024} KB）由 {uploader} 上传（{ip}）", "info")
    return {"ok": True, "id": iid, "size": len(raw), "status": "pending"}


@remote_router.post("/remote/upload-file")
async def remote_upload_file(request: Request, file: UploadFile = File(...)):
    """远程 multipart 文件上传（需登录账号，图片进入待审核）。"""
    ip = await _guard_remote_upload(request)
    uploader = _upload_user_from(request)
    name = Path(file.filename or "remote.png").name[:40] or "remote.png"
    ext = Path(name).suffix.lower()
    if ext not in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"):
        raise HTTPException(400, "仅支持 png/jpg/jpeg/gif/webp/bmp")
    fname = f"{name.rsplit(ext, 1)[0][:20]}-{uuid.uuid4().hex}{ext}"
    dest = db.UPLOAD_DIR / fname
    size = 0
    try:
        with dest.open("wb") as f:
            while chunk := await file.read(1 << 20):
                size += len(chunk)
                if size > _REMOTE_MAX_FILE_MB * 1024 * 1024:
                    raise HTTPException(413, f"文件超过 {_REMOTE_MAX_FILE_MB}MB 限制")
                f.write(chunk)
    except HTTPException:
        dest.unlink(missing_ok=True)
        raise
    h = hashlib.sha256(dest.read_bytes()).hexdigest()
    dup = db.find_image_by_hash(h)
    if dup:
        dest.unlink(missing_ok=True)
        raise HTTPException(409, f"图片重复：图库中已存在相同内容的图片（#{dup['id']}），已被自动拒绝")
    iid = db.add_image(fname, name, size, status="pending", uploader=uploader, hash=h)
    db.add_log(f"待审核图片 {name}（{size // 1024} KB）由 {uploader} 上传（{ip}）", "info")
    return {"ok": True, "id": iid, "size": size, "status": "pending"}


class UploadLoginIn(BaseModel):
    username: str
    password: str

class ApplyStartIn(BaseModel):
    mail: str

class ApplyDoneIn(BaseModel):
    mail: str
    code: str
    username: str
    password: str


@remote_router.post("/remote/apply/send-code")
async def remote_apply_send_code(request: Request, body: ApplyStartIn):
    """发送注册验证码到 QQ 邮箱（限流保护）。"""
    ip = _client_ip(request)
    ok, reason = _rate_check(ip)
    if not ok:
        raise HTTPException(429, reason)
    try:
        ttl = mailer.send_code(body.mail)
    except mailer.MailError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "ttl": ttl}


@remote_router.post("/remote/apply")
async def remote_apply(request: Request, body: ApplyDoneIn):
    """提交注册：邮箱验证码通过即自动创建账号（一个邮箱一个账号）。"""
    ip = _client_ip(request)
    ok, reason = _rate_check(ip)
    if not ok:
        raise HTTPException(429, reason)
    if not mailer._valid_qq_mail(body.mail):
        raise HTTPException(400, "仅支持 QQ 邮箱")
    if not mailer.verify_code(body.mail, body.code):
        raise HTTPException(400, "验证码错误或已过期")
    username = body.username.strip()
    if not re.fullmatch(r"[A-Za-z0-9_\u4e00-\u9fff]{2,20}", username):
        raise HTTPException(400, "账号名需 2-20 位字母/数字/下划线/中文")
    if len(body.password) < 4:
        raise HTTPException(400, "密码至少 4 位")
    # 一个邮箱只能注册一个账号
    for a in db.list_applies():
        if a["mail"] == body.mail.strip().lower() and a["status"] in ("pending", "approved"):
            raise HTTPException(400, "该邮箱已注册过账号")
    # 验证码通过即自动生效：先落申请记录（留档），再直接建账号
    aid = db.create_apply(body.mail, username, body.password)
    if aid is None:
        raise HTTPException(400, "账号名已被使用，换一个账号名")
    uid = db.approve_apply(aid)
    if uid is None:
        db.set_apply_status(aid, "rejected")
        raise HTTPException(400, "账号名已被使用，换一个账号名")
    # 注册赠送 500 积分
    with db.db() as conn:
        conn.execute("UPDATE upload_users SET points=points+500 WHERE username=?", (username,))
    db.add_log(f"邮箱注册自动通过：{username}（{body.mail}），赠送 500 积分", "info")
    rec = db.get_upload_user(username)
    return {"ok": True, "aid": aid, "auto": True,
            "uid_number": (rec or {}).get("uid_number") or "",
            "msg": "注册成功！邮箱验证通过，账号已生效，可直接登录"}


@remote_router.get("/remote/apply/status")
async def remote_apply_status(request: Request, mail: str):
    """申请者查询自己申请的审批状态。"""
    ip = _client_ip(request)
    ok, reason = _rate_check(ip)
    if not ok:
        raise HTTPException(429, reason)
    mail = (mail or "").strip().lower()
    for a in db.list_applies():
        if a["mail"] == mail:
            return {"ok": True, "status": a["status"], "username": a["username"]}
    return {"ok": True, "status": "none"}


class CheckinIn(BaseModel):
    username: str = ""   # 可选；默认用登录态


@remote_router.get("/remote/activities")
async def remote_activities(request: Request):
    """当前生效中的积分活动公告（用户端展示）。"""
    ip = _client_ip(request)
    ok, reason = _rate_check(ip)
    if not ok:
        raise HTTPException(429, reason)
    acts = _acts()
    acts._ensure_activities_safe()
    out = [{"title": a["title"], "body": a["body"], "multiplier": a["multiplier"]}
           for a in acts.active_activities()]
    return {"ok": True, "activities": out, "multiplier": acts.current_multiplier()}


@remote_router.post("/remote/checkin")
async def remote_checkin(request: Request, body: CheckinIn):
    """每日签到（需登录态）。"""
    ip = _client_ip(request)
    ok, reason = _rate_check(ip)
    if not ok:
        raise HTTPException(429, reason)
    u = _upload_user_from(request)
    r = checkin.checkin(u)
    return r


@remote_router.get("/remote/points")
async def remote_points(request: Request):
    """我的积分 / 管理员状态 / 兑换档位。"""
    ip = _client_ip(request)
    ok, reason = _rate_check(ip)
    if not ok:
        raise HTTPException(429, reason)
    u = _upload_user_from(request)
    return checkin.get_points(u)


class ExchangeIn(BaseModel):
    tier: int


@remote_router.post("/remote/exchange")
async def remote_exchange(request: Request, body: ExchangeIn):
    """积分兑换管理员权限（时间制）。"""
    ip = _client_ip(request)
    ok, reason = _rate_check(ip)
    if not ok:
        raise HTTPException(429, reason)
    u = _upload_user_from(request)
    r = checkin.exchange(u, body.tier)
    if r.get("ok"):
        db.add_log(f"{u} 用 {r['spent']} 积分兑换管理员权限至 {r['admin_until']}", "info")
    return r


@remote_router.post("/remote/login")
async def remote_login(request: Request, body: UploadLoginIn):
    ip = _client_ip(request)
    ok, reason = _rate_check(ip)
    if not ok:
        raise HTTPException(429, reason)
    if not db.verify_upload_user(body.username.strip(), body.password):
        raise HTTPException(401, "账号或密码错误")
    # 封禁检查
    rec = db.get_upload_user(body.username.strip())
    if rec:
        banned, until, reason = db.ban_status(rec)
        if banned:
            tip = "账号已被封禁"
            if until and until != "永久":
                tip += f"，解封时间 {until}"
            else:
                tip += "（永久）"
            if reason:
                tip += f"。原因：{reason}"
            raise HTTPException(403, tip)
    tok = _make_upload_token(body.username.strip())
    rec = db.get_upload_user(body.username.strip())
    pts = checkin.get_points(body.username.strip())
    resp = JSONResponse({"ok": True, "username": body.username.strip(),
                         "uid_number": (rec or {}).get("uid_number") or "",
                         "is_admin": bool((rec or {}).get("is_admin")),
                         "is_timed_admin": pts.get("is_timed_admin", False),
                         "points": pts.get("points", 0),
                         "today_checked": pts.get("today_checked", False),
                         "token": tok})
    resp.set_cookie("upload_session", tok, httponly=True, samesite="lax",
                    max_age=_UPLOAD_SESSION_HOURS * 3600)
    return resp

@remote_router.post("/remote/logout")
async def remote_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("upload_session")
    return resp

@remote_router.get("/remote/me")
async def remote_me(request: Request):
    u = _upload_user_from(request)
    rec = db.get_upload_user(u)
    pts = checkin.get_points(u)
    return {"ok": True, "username": u,
            "uid_number": (rec or {}).get("uid_number") or "",
            "is_admin": bool((rec or {}).get("is_admin")),
            "is_timed_admin": pts.get("is_timed_admin", False),
            "admin_until": pts.get("admin_until", ""),
            "points": pts.get("points", 0),
            "today_checked": pts.get("today_checked", False),
            "id": (rec or {}).get("id")}

@remote_router.get("/remote/mine")
async def remote_mine(request: Request):
    """我上传的图片及审核状态。"""
    u = _upload_user_from(request)
    return {"ok": True, "images": db.list_images(uploader=u)}


class ChangePwIn(BaseModel):
    old_password: str
    new_password: str


@remote_router.post("/remote/change-password")
async def remote_change_password(request: Request, body: ChangePwIn):
    """上传者自行修改密码：需校验旧密码。"""
    u = _upload_user_from(request)
    if not db.verify_upload_user(u, body.old_password):
        raise HTTPException(401, "原密码错误")
    new_pw = body.new_password.strip()
    if len(new_pw) < 4:
        raise HTTPException(400, "新密码至少 4 位")
    if new_pw == body.old_password:
        raise HTTPException(400, "新密码不能与原密码相同")
    uid = db.get_upload_user(u)["id"]
    db.update_upload_user(uid, password=new_pw)
    db.add_log(f"上传账号 {u} 修改了自己的密码", "info")
    # 改密后强制重新登录
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("upload_session")
    return resp

class AdminReviewIn(BaseModel):
    admin_password: str
    ids: list[int]
    action: str          # approve | reject


class RemoteListIn(BaseModel):
    admin_password: str = ""
    status: str | None = None      # approved/pending/rejected/None=全部


@remote_router.post("/remote/admin-images")
async def remote_admin_images(request: Request, body: RemoteListIn):
    """管理员查看图片库全部图片（8001 管理面板用）。"""
    ip = _client_ip(request)
    _require_admin_ctx(request,     _require_admin_ctx(request, body.admin_password)
)
    ok, reason = _rate_check(ip)
    if not ok:
        raise HTTPException(429, reason)
    imgs = db.list_images(status=body.status) if body.status else db.list_images()
    return {"ok": True, "images": [
        {"id": i["id"], "original": i["original"], "uploader": i["uploader"],
         "status": i["status"], "size": i["size"], "created_at": i["created_at"]}
        for i in imgs]}


class RemoteDeleteIn(BaseModel):
    admin_password: str = ""
    ids: list[int] = []


@remote_router.post("/remote/admin-delete")
async def remote_admin_delete(request: Request, body: RemoteDeleteIn):
    """管理员删除图片（8001 管理面板用）。"""
    ip = _client_ip(request)
    _require_admin_ctx(request,     _require_admin_ctx(request, body.admin_password)
)
    ok, reason = _rate_check(ip)
    if not ok:
        raise HTTPException(429, reason)
    if not body.ids:
        raise HTTPException(400, "未选择图片")
    who = "管理员账号" if (lambda t: db.get_upload_user(t) if t else None)(
        (request.headers.get("X-Upload-Session", "").strip()
         or request.cookies.get("upload_session", ""))) else "管理密码"
    deleted, missing = 0, 0
    for iid in body.ids:
        img = db.get_image(int(iid))
        if not img:
            missing += 1
            continue
        db.delete_image(int(iid))
        (db.UPLOAD_DIR / img["filename"]).unlink(missing_ok=True)
        (THUMB_DIR / (Path(img["filename"]).stem + ".webp")).unlink(missing_ok=True)
        deleted += 1
    db.add_log(f"8001 {who}删除图片 {deleted} 张", "warn" if deleted else "info")
    return {"ok": True, "deleted": deleted, "missing": missing}


@remote_router.post("/remote/admin-review")
async def remote_admin_review(request: Request, body: AdminReviewIn):
    """管理员在 8001 上传页直接审批待审图片（用管理密码鉴权）。"""
    ip = _client_ip(request)
    _require_admin_ctx(request,     _require_admin_ctx(request, body.admin_password)
)
    ok, reason = _rate_check(ip)
    if not ok:
        raise HTTPException(429, reason)
    if body.action not in ("approve", "reject"):
        raise HTTPException(400, "action 无效")
    status = "approved" if body.action == "approve" else "rejected"
    n = db.bulk_review([int(i) for i in body.ids if str(i).isdigit()], status)
    db.add_log(f"管理员（8001 页面）审核图片 {n} 张 → {status}", "info")
    return {"ok": True, "count": n}


@remote_router.get("/remote/admin-pending")
@remote_router.post("/remote/admin-pending")
async def remote_admin_pending(request: Request, admin_password: str = ""):
    """管理员查看待审图片列表（8001 页面审批用）。GET/POST 均可。"""
    pw = admin_password
    if request.method == "POST":
        try:
            body = await request.json()
            pw = str(body.get("admin_password") or "")
        except Exception:
            pw = ""
    _require_admin_ctx(request, pw)
    ip = _client_ip(request)
    ok, reason = _rate_check(ip)
    if not ok:
        raise HTTPException(429, reason)
    out = []
    for i in db.list_images(status="pending"):
        out.append({"id": i["id"], "original": i["original"], "uploader": i["uploader"], "created_at": i["created_at"]})
    return {"ok": True, "images": out}


@remote_router.get("/remote/admin-applies")
@remote_router.post("/remote/admin-applies")
async def remote_admin_applies(request: Request, admin_password: str = ""):
    """管理员查看待批注册申请（8001 审批面板用）。GET/POST 均可。"""
    pw = admin_password
    if request.method == "POST":
        try:
            body = await request.json()
            pw = str(body.get("admin_password") or "")
        except Exception:
            pw = ""
    _require_admin_ctx(request, pw)
    ip = _client_ip(request)
    ok, reason = _rate_check(ip)
    if not ok:
        raise HTTPException(429, reason)
    return {"ok": True, "applies": db.list_applies("pending")}


@remote_router.post("/remote/admin-apply-approve")
async def remote_admin_apply_approve(request: Request, body: dict):
    """管理员在 8001 页面批准注册申请。"""
    ip = _client_ip(request)
    _require_admin_ctx(request,     _require_admin_ctx(request, str(body.get("admin_password", "")))
)
    ok, reason = _rate_check(ip)
    if not ok:
        raise HTTPException(429, reason)
    uid = db.approve_apply(int(body.get("aid", 0)))
    if uid is None:
        raise HTTPException(400, "申请不存在、已处理或用户名冲突")
    a = db.get_apply(int(body.get("aid", 0)))
    db.add_log(f"管理员（8001 页面）批准注册申请：{a['username']}", "info")
    return {"ok": True, "uid": uid}


@remote_router.post("/remote/admin-apply-reject")
async def remote_admin_apply_reject(request: Request, body: dict):
    ip = _client_ip(request)
    _require_admin_ctx(request,     _require_admin_ctx(request, str(body.get("admin_password", "")))
)
    ok, reason = _rate_check(ip)
    if not ok:
        raise HTTPException(429, reason)
    aid = int(body.get("aid", 0))
    a = db.get_apply(aid)
    if not a or a["status"] != "pending":
        raise HTTPException(400, "申请不存在或已处理")
    db.set_apply_status(aid, "rejected")
    return {"ok": True}


@remote_router.get("/remote/ping")
async def remote_ping(request: Request):
    """连通性检查：带正确 Token 即返回 ok（同样受限流保护）。"""
    ip = _client_ip(request)
    ok, reason = _rate_check(ip)
    if not ok:
        raise HTTPException(429, reason)
    expected = db.get_settings().get("remote_token") or ""
    got = (request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
           or request.headers.get("X-Upload-Token", "").strip())
    if expected and not secrets.compare_digest(got, expected):
        raise HTTPException(401, "上传 Token 无效")
    return {"ok": True, "images": len(db.list_images(only_approved=True)), "pending": len(db.list_images(status="pending"))}


# ---------------- 日志 ----------------


@app.get("/api/logs")
async def api_logs(limit: int = 200):
    return db.list_logs(limit)


@app.delete("/api/logs")
async def api_clear_logs():
    db.clear_logs()
    return {"ok": True}


@app.get("/api/events")
async def api_events(request: Request):
    """SSE：把新日志实时推给页面。"""

    async def gen():
        last_id = 0
        while True:
            if await request.is_disconnected():
                break
            rows = db.list_logs(50)
            new = [r for r in rows if r["id"] > last_id]
            new.reverse()
            for r in new:
                last_id = r["id"]
                yield "data: " + json.dumps(r, ensure_ascii=False) + "\n\n"
            await asyncio.sleep(3)

    return Response(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _tz():
    from zoneinfo import ZoneInfo

    return ZoneInfo("Asia/Shanghai")


@app.on_event("startup")
async def _startup():
    scheduler.start()
    if not db.list_tasks():
        # 首次启动给一个示例任务（默认停用，避免误发）
        tid = db.create_task(
            {
                "name": "每日早安（示例）",
                "enabled": False,
                "content": "早上好呀！今天是 {date} {weekday}，祝大家一切顺利 🌞",
                "schedule": "0 8 * * *",
                "timezone": "Asia/Shanghai",
                "targets": [],
            }
        )
        db.add_log(f"已创建示例任务 #{tid}（默认停用，设置目标和群后手动启用）", "info", tid)
    log.info("服务启动，端口 %s", PORT)
    if not _auth_required():
        log.warning("当前未设置管理密码，任何人访问端口都能操作，建议在“设置”里立即设置！")


@app.on_event("shutdown")
async def _shutdown():
    scheduler.shutdown()


app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


# ========== 独立上传服务（单独端口暴露，不包含后台任何功能） ==========

# 从主应用摘出远程上传相关的路由，组成一个精简 app
remote_app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


@remote_app.get("/")
@remote_app.get("/upload")
async def remote_upload_page():
    """纯上传页面：手机/电脑浏览器直接打开即可传图。"""
    return HTMLResponse((BASE_DIR / "static" / "upload.html").read_text(encoding="utf-8"))


@remote_router.get("/remote/file/{image_id}")
async def remote_file(image_id: int, request: Request, token: str = ""):
    """原图查看（大图/放大）：本人或管理员。支持 ?token= 查询参数。"""
    tok = (request.headers.get("X-Upload-Session", "").strip()
           or token.strip()
           or request.cookies.get("upload_session", ""))
    u = _parse_upload_token(tok) if tok else None
    if not u:
        raise HTTPException(401, "请先登录")
    img = db.get_image(image_id)
    rec = db.get_upload_user(u)
    is_admin = bool(rec and rec.get("is_admin"))
    if not img or (img.get("uploader") != u and not is_admin):
        raise HTTPException(404, "图片不存在")
    src = db.UPLOAD_DIR / img["filename"]
    if not src.exists():
        raise HTTPException(404, "文件丢失")
    from main import _guess_mime
    return Response(src.read_bytes(), media_type=_guess_mime(img["filename"]))


@remote_router.get("/remote/thumb/{image_id}")
async def remote_thumb(image_id: int, request: Request, token: str = ""):
    """缩略图：本人可见自己的图；管理员可见全部。img 标签无法带 header，
    支持 ?token= 查询参数鉴权。"""
    # 鉴权：header > 查询参数 > cookie（显式 token 优先，避免旧 cookie 干扰管理员视图）
    tok = (request.headers.get("X-Upload-Session", "").strip()
           or token.strip()
           or request.cookies.get("upload_session", ""))
    u = _parse_upload_token(tok) if tok else None
    if not u:
        raise HTTPException(401, "请先登录")
    img = db.get_image(image_id)
    rec = db.get_upload_user(u)
    is_admin = bool(rec and rec.get("is_admin"))
    # 本人或管理员可见
    if not img or (img.get("uploader") != u and not is_admin):
        raise HTTPException(404, "图片不存在")
    src = db.UPLOAD_DIR / img["filename"]
    if not src.exists():
        raise HTTPException(404, "文件丢失")
    thumb = _ensure_thumb(img)
    data = thumb.read_bytes() if thumb and thumb.exists() else src.read_bytes()
    media = "image/webp" if thumb and thumb.exists() else _guess_mime(img["filename"])
    return Response(data, media_type=media, headers={"Cache-Control": "private, max-age=300"})


# 挂载所有 /remote/* 路由（必须在全部路由定义之后）
remote_app.include_router(remote_router)


@remote_app.middleware("http")
async def remote_404_guard(request: Request, call_next):
    """只放行 / 与 /remote/*、静态上传页资源，其余一律 404，防探测。"""
    path = request.url.path
    allowed = (
        path == "/"
        or path == "/upload"
        or path.startswith("/remote")
        or path == "/static/upload.html"   # 页面自身无需额外资源，但保留以防万一
    )
    if not allowed:
        return JSONResponse({"detail": "Not Found"}, status_code=404)
    return await call_next(request)


if __name__ == "__main__":
    # 同时跑两个端口：主后台(8080) + 独立上传服务(8001，只含 /remote/*)
    # 用子进程方式最稳（uvicorn 多 app 单进程不支持）
    import subprocess
    import sys as _sys
    import uvicorn

    remote_proc = subprocess.Popen(
        [_sys.executable, "-m", "uvicorn", "main:remote_app",
         "--host", "0.0.0.0", "--port", str(REMOTE_PORT), "--log-level", "warning"],
        cwd=str(BASE_DIR),
    )
    try:
        uvicorn.run("main:app", host="0.0.0.0", port=PORT, log_level="info")
    finally:
        remote_proc.terminate()


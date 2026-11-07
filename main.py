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
    return (BASE_DIR / "static" / "index.html").read_text(encoding="utf-8")


# ---------------- 设置 / 状态 ----------------


class SettingsIn(BaseModel):
    onebot_http: str | None = None
    onebot_token: str | None = None
    post_token: str | None = None
    remote_token: str | None = None
    admin_password: str | None = None
    rss_timeout: int | None = None


@app.get("/api/settings")
async def api_get_settings():
    s = db.get_settings()
    s["onebot_token"] = "******" if s.get("onebot_token") else ""
    s["post_token"] = "******" if s.get("post_token") else ""
    s["remote_token"] = "******" if s.get("remote_token") else ""
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
    if body.rss_timeout is not None:
        data["rss_timeout"] = str(body.rss_timeout)
    db.set_settings(data)
    return {"ok": True}


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
    expected = db.get_settings().get("post_token") or ""
    if expected:
        got = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
        if got != expected:
            return {"ok": False}
    try:
        event = await request.json()
    except Exception:
        return {"ok": False}
    if event.get("post_type") == "message":
        triggers.handle_event_async(event)
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
    if len(items) > 2000:
        raise HTTPException(400, "最多 2000 条")
    db.set_settings({"quotes": json.dumps(items, ensure_ascii=False)})
    db.add_log(f"更新自定义名言池（{len(items)} 条）", "info")
    return {"ok": True, "count": len(items)}


@app.delete("/api/quotes")
async def api_reset_quotes():
    db.set_settings({"quotes": ""})
    db.add_log("名言池已恢复默认", "info")
    return {"ok": True}


# ---------------- 图片库 ----------------


@app.get("/api/images")
async def api_images():
    return db.list_images()


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
    iid = db.add_image(name, file.filename or name, size)
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
    resp.headers["Cache-Control"] = "max-age=86400"  # 文件名是随机 uuid，可放心长缓存
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

# 滑动窗口限流：每 IP 在窗口期内最多 N 次请求
_RATE_LOCK = threading.Lock()
_RATE_WINDOWS: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=64))
_RATE_LIMIT = 20          # 每窗口期最多请求次数
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
    """远程 JSON/base64 上传：POST /remote/upload，Token 见设置。"""
    ip = await _guard_remote_upload(request)
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
    iid = db.add_image(fname, name, len(raw))
    db.add_log(f"远程上传图片 {name}（{len(raw) // 1024} KB）来自 {ip}", "info")
    return {"ok": True, "id": iid, "size": len(raw)}


@remote_router.post("/remote/upload-file")
async def remote_upload_file(request: Request, file: UploadFile = File(...)):
    """远程 multipart 文件上传：POST /remote/upload-file。"""
    ip = await _guard_remote_upload(request)
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
    iid = db.add_image(fname, name, size)
    db.add_log(f"远程上传图片 {name}（{size // 1024} KB）来自 {ip}", "info")
    return {"ok": True, "id": iid, "size": size}


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
    return {"ok": True, "images": len(db.list_images())}


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
remote_app.include_router(remote_router)


@remote_app.get("/", response_class=HTMLResponse)
@remote_app.get("/upload", response_class=HTMLResponse)
async def remote_upload_page():
    """纯上传页面：手机/电脑浏览器直接打开即可传图。"""
    return (BASE_DIR / "static" / "upload.html").read_text(encoding="utf-8")


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


"""看群友发的图、存表情、斗图。

三个能力：
1. 看图：把消息里的图片段下载下来、转成 PNG，交给视觉模型描述；
2. 存表情：判断这张图「值得偷」，存进表情库（带自己写的备注和标签）；
3. 斗图：从表情库里挑一张对味的发出去，看到别人表情就回一张。

存的东西：
- 文件放 data/stickers/xxx.png
- 元数据放 SQLite 的 stickers 表（备注、标签、用法、使用次数）
"""
from __future__ import annotations

import base64
import io
import logging
import time
from typing import Any

import httpx

import db
import onebot

log = logging.getLogger("qqbot.stickers")

# 单张图上限（base64 后别把模型上下文撑爆）
MAX_IMAGE_BYTES = 8 * 1024 * 1024
# 送模型前缩到这个尺寸（越小越不容易被上游拒）
VISION_MAX_SIDE = 448


# ---------------- 取图 ----------------


def segment_url(seg: dict) -> str:
    """从图片消息段里取下载地址。"""
    data = seg.get("data") or {}
    return str(data.get("url") or data.get("file") or "")


def fetch_image(seg: dict, timeout: float = 25.0) -> tuple[bytes, str] | None:
    """下载图片，返回 (二进制, 内容类型)。失败返回 None。"""
    data = seg.get("data") or {}
    url = str(data.get("url") or "")

    # 优先直连 URL（实测可用）；不行再让 NapCat 从本地缓存取
    if url.startswith("http"):
        try:
            with httpx.Client(timeout=timeout, trust_env=False, follow_redirects=True) as c:
                r = c.get(url)
            if r.status_code == 200 and r.content:
                return r.content, r.headers.get("content-type") or "image/jpeg"
        except httpx.HTTPError as e:
            log.warning("图片直连下载失败: %s", e)

    # 兜底：NapCat get_image 会返回容器内路径，读文件
    try:
        info = onebot.call("get_image", {"file": data.get("file") or ""}) or {}
        path = info.get("file") or ""
        if path:
            raw = onebot.read_container_file(path)
            if raw:
                return raw, "image/png"
    except Exception as e:
        log.warning("get_image 兜底失败: %s", e)
    return None


def to_png(raw: bytes, max_side: int = VISION_MAX_SIDE) -> bytes | None:
    """转成 PNG（视觉模型不认 GIF）。动图取第一帧。"""
    try:
        from PIL import Image
    except ImportError:
        return raw
    try:
        im = Image.open(io.BytesIO(raw))
        try:
            im.seek(0)
        except Exception:
            pass
        im = im.convert("RGB")
        if max(im.size) > max_side:
            im.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        im.save(buf, "PNG")
        return buf.getvalue()
    except Exception as e:
        log.warning("图片转 PNG 失败: %s", e)
        return None


def as_data_url(raw: bytes, mime: str = "image/png") -> str:
    return f"data:{mime};base64," + base64.b64encode(raw).decode()


# ---------------- 看图 ----------------


def describe(seg: dict, hint: str = "") -> str:
    """让视觉模型描述这张图。失败返回空串。"""
    got = fetch_image(seg)
    if not got:
        return ""
    raw, _mime = got
    if len(raw) > MAX_IMAGE_BYTES:
        log.warning("图片过大 %d 字节，跳过", len(raw))
        return ""
    png = to_png(raw)
    if not png:
        return ""

    import ai as ai_mod

    prompt = (
        "看看这张图，用一句中文说清楚它是什么/画了什么，顺便判断它是不是表情包。"
        "不要客套，不要解释你在做什么。格式：内容描述 + 一个词概括情绪（比如 无语/嘲笑/赞同/卖萌）。"
    )
    if hint:
        prompt += f"\n群里这句话是：{hint}"

    reply = ai_mod.vision_chat(prompt, as_data_url(png))
    return (reply or "").strip()


# ---------------- 表情库 ----------------


def is_sticker_like(seg: dict) -> bool:
    """消息段像不像表情包。"""
    data = seg.get("data") or {}
    if seg.get("type") == "mface":
        return True
    if seg.get("type") == "image":
        # sub_type=1 一般是收藏表情/动画表情
        try:
            if int(data.get("sub_type") or 0) == 1:
                return True
        except (TypeError, ValueError):
            pass
        summary = str(data.get("summary") or "")
        return "表情" in summary
    return False


def save_sticker(seg: dict, note: str, tags: str = "", usage: str = "") -> int | None:
    """把图片段存进表情库。返回 id，失败 None。"""
    got = fetch_image(seg)
    if not got:
        return None
    raw, _mime = got
    if len(raw) > MAX_IMAGE_BYTES:
        return None
    png = to_png(raw, max_side=480)
    if not png:
        return None
    try:
        sid = db.add_sticker(png, note=note[:120], tags=tags[:80], usage=usage[:120])
        log.info("存了一张表情 #%s：%s", sid, note)
        return sid
    except Exception as e:
        log.warning("存表情失败: %s", e)
        return None


def random_sticker(exclude_recent: int = 8) -> dict[str, Any] | None:
    """随机挑一张表情（尽量别总挑同一张）。"""
    return db.pick_sticker(exclude_recent=exclude_recent)


def send_sticker(seg_type: str, target_id: int, sticker: dict[str, Any]) -> bool:
    """把表情库里的一张图发出去。"""
    from pathlib import Path
    try:
        path = Path(db.sticker_path(sticker))
        if not path.is_file():
            log.warning("表情文件不存在: %s", path)
            return False
        onebot.send_message(seg_type, target_id, [onebot.image_segment(path)])
        db.touch_sticker(int(sticker["id"]))
        return True
    except Exception as e:
        log.warning("发表情失败: %s", e)
        return False


def list_for_prompt(limit: int = 12) -> str:
    """给模型看的表情清单：有备注的优先。"""
    rows = db.list_stickers(limit=limit)
    if not rows:
        return "（表情库还是空的）"
    lines = []
    for r in rows:
        note = r.get("note") or "未命名"
        tags = r.get("tags") or ""
        lines.append(f"#{r['id']} {note}" + (f"（{tags}）" if tags else ""))
    return "\n".join(lines)

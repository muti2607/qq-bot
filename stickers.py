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
    """转成 PNG（视觉模型不认 GIF）。动图只取第一帧，仅用于「看图」。"""
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


def keep_original(raw: bytes, max_side: int = 480) -> tuple[bytes, str]:
    """存表情用：保留动图。返回 (二进制, 扩展名)。

    - GIF：原样保留（缩小会掉帧/变大，得不偿失），直接存 .gif
    - 其它：转 PNG，长边缩到 max_side
    """
    try:
        from PIL import Image
    except ImportError:
        return raw, "png"

    try:
        im = Image.open(io.BytesIO(raw))
        fmt = (im.format or "").upper()
        n_frames = getattr(im, "n_frames", 1) or 1

        # 动图原样保留
        if fmt == "GIF" and n_frames > 1:
            return raw, "gif"
        if fmt == "WEBP" and n_frames > 1:
            return raw, "webp"

        # 静态图：转 PNG 并缩放
        im.seek(0)
        im = im.convert("RGBA" if fmt in ("PNG", "WEBP") else "RGB")
        if max(im.size) > max_side:
            im.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        im.save(buf, "PNG")
        return buf.getvalue(), "png"
    except Exception as e:
        log.warning("保留原图失败，按 PNG 处理: %s", e)
        return raw, "png"


def is_animated(raw: bytes) -> bool:
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(raw))
        return (getattr(im, "n_frames", 1) or 1) > 1
    except Exception:
        return False


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
    """把图片段存进表情库。

    按图片内容 md5 去重：同一张图不管备注写得多不一样，都只存一份。
    动图保留 GIF 原样，不会被压成静态图。
    返回 id（可能是已存在的那个）；失败返回 None。
    """
    got = fetch_image(seg)
    if not got:
        return None
    raw, _mime = got
    if len(raw) > MAX_IMAGE_BYTES:
        return None
    data, ext = keep_original(raw, max_side=480)
    if not data:
        return None
    try:
        sid = db.add_sticker(data, note=note[:120], tags=tags[:80], usage=usage[:120], ext=ext)
        if sid:
            log.info("存表情 #%s（%s，%d 字节）：%s", sid, ext, len(data), note)
        return sid
    except Exception as e:
        log.warning("存表情失败: %s", e)
        return None


def already_saved(seg: dict) -> bool:
    """这张图是不是已经在库里了（按内容哈希判断）。"""
    got = fetch_image(seg)
    if not got:
        return False
    data, _ext = keep_original(got[0], max_side=480)
    if not data:
        return False
    import hashlib
    return db.find_sticker_by_hash(hashlib.md5(data).hexdigest()) is not None


def random_sticker(exclude_recent: int = 8) -> dict[str, Any] | None:
    """随机挑一张表情（尽量别总挑同一张）。"""
    return db.pick_sticker(exclude_recent=exclude_recent)


def send_sticker(seg_type: str, target_id: int, sticker: dict[str, Any]) -> bool:
    """把表情库里的一张图发出去（动图原样发，开关关掉时取首帧转静态）。"""
    from pathlib import Path
    try:
        path = Path(db.sticker_path(sticker))
        if not path.is_file():
            log.warning("表情文件不存在: %s", path)
            return False

        data = path.read_bytes()
        animated = is_animated(data)
        send_static_only = str(db.get_settings().get("ai_send_animated", "1")) != "1"

        if animated and send_static_only:
            # 只发静态：取第一帧存临时 PNG
            png = to_png(data, max_side=480)
            if not png:
                return False
            import tempfile
            tmp = Path(tempfile.gettempdir()) / f"st-{sticker['id']}.png"
            tmp.write_bytes(png)
            onebot.send_message(seg_type, target_id, [onebot.image_segment(tmp)])
        else:
            onebot.send_message(seg_type, target_id, [onebot.image_segment(path)])

        db.touch_sticker(int(sticker["id"]))
        if animated:
            log.info("发出动图 #%s", sticker["id"])
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


# ---------------- 收藏到 QQ 收藏表情 ----------------


def copy_to_qq(data: bytes, ext: str) -> str | None:
    """把图片字节塞到 NapCat 容器内可见的位置，返回容器内路径。

    NapCat 的 add_custom_face 只认它自己容器里的文件路径。
    napcat 容器只挂载了 config 和 qq 两个子目录（不是整个 /app/napcat），
    所以必须借用 config 目录中转：
      宿主机 /opt/qq-bot/napcat/config/favtmp  ==  容器 /app/napcat/config/favtmp
    机器人与 napcat 是兄弟容器，机器人这边靠自己的 /app 挂载写到同一份宿主机目录。
    """
    import secrets as _secrets
    from pathlib import Path

    host_dir = Path("/app/napcat/config/favtmp")      # 机器人容器内 = 宿主机 /opt/qq-bot/napcat/config/favtmp
    try:
        host_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        log.warning("建临时目录失败: %s", e)
        return None

    name = "fav-" + _secrets.token_hex(6) + "." + (ext or "png")
    try:
        (host_dir / name).write_bytes(data)
    except OSError as e:
        log.warning("写临时表情失败: %s", e)
        return None
    return "/app/napcat/config/favtmp/" + name


def _cleanup_qqtmp(remote: str) -> None:
    from pathlib import Path
    try:
        Path("/app/napcat/config/favtmp").joinpath(Path(remote).name).unlink(missing_ok=True)
    except Exception:
        pass


def favorite_to_qq(sticker: dict[str, Any]) -> bool:
    """把库里的一张表情收藏进 QQ 的「收藏表情」。"""
    from pathlib import Path
    try:
        src = Path(db.sticker_path(sticker))
        if not src.is_file():
            log.warning("收藏失败，文件不存在: %s", src)
            return False
        data = src.read_bytes()
        ext = src.suffix.lstrip(".") or "png"

        remote = copy_to_qq(data, ext)
        if not remote:
            return False
        try:
            onebot.call("add_custom_face", {"file": remote})
        finally:
            _cleanup_qqtmp(remote)
        log.info("已收藏到 QQ 表情：%s", sticker.get("note") or sticker.get("id"))
        return True
    except Exception as e:
        log.warning("收藏到 QQ 失败: %s", e)
        return False


def favorite_segment_to_qq(seg: dict) -> bool:
    """把消息里的一个图片段直接收藏进 QQ 表情。"""
    got = fetch_image(seg)
    if not got:
        return False
    data, ext = keep_original(got[0], max_side=480)
    if not data:
        return False
    remote = copy_to_qq(data, ext)
    if not remote:
        return False
    try:
        onebot.call("add_custom_face", {"file": remote})
        log.info("已把群友的图收藏到 QQ 表情（%s）", ext)
        return True
    except Exception as e:
        log.warning("收藏到 QQ 失败: %s", e)
        return False
    finally:
        _cleanup_qqtmp(remote)


def qq_favorites() -> list[str]:
    """读取 QQ 收藏表情的 URL 列表。"""
    try:
        data = onebot.call("fetch_custom_face") or []
        return [str(x) for x in data] if isinstance(data, list) else []
    except Exception as e:
        log.warning("读取 QQ 收藏表情失败: %s", e)
        return []


# ---------------- 直接用 QQ 收藏表情发图 ----------------

# 收藏表情的 URL 缓存（避免每次都问 NapCat）
_FAV_CACHE: dict[str, Any] = {"at": 0.0, "urls": []}
_FAV_TTL = 300.0


def _fav_urls(force: bool = False) -> list[str]:
    now = time.time()
    if not force and _FAV_CACHE["urls"] and now - _FAV_CACHE["at"] < _FAV_TTL:
        return list(_FAV_CACHE["urls"])
    urls = qq_favorites()
    if urls:
        _FAV_CACHE["urls"] = urls
        _FAV_CACHE["at"] = now
    return urls


def _download(url: str, timeout: float = 20.0) -> bytes | None:
    try:
        with httpx.Client(timeout=timeout, trust_env=False, follow_redirects=True) as c:
            r = c.get(url)
        if r.status_code == 200 and len(r.content) > 100:
            return r.content
        log.warning("收藏表情下载失败 HTTP %s", r.status_code)
    except httpx.HTTPError as e:
        log.warning("收藏表情下载出错: %s", e)
    return None


def send_qq_favorite(seg_type: str, target_id: int) -> bool:
    """从 QQ 收藏表情里随机挑一张发出去（动图原样发）。

    这是「用你自己 QQ 里收藏的表情」的入口——不经过本地表情库。
    """
    urls = _fav_urls()
    if not urls:
        # 缓存可能空了（刚清空收藏），强制再问一次 NapCat
        urls = _fav_urls(force=True)
    if not urls:
        log.warning("QQ 收藏表情是空的，没法发")
        return False

    # 随机挑，避开最近刚发过的
    import random
    recent = _FAV_CACHE.setdefault("recent", [])
    pool = [u for u in urls if u not in recent] or urls
    url = random.choice(pool)
    recent.append(url)
    if len(recent) > 8:
        del recent[:-8]

    data = _download(url)
    if not data:
        return False

    animated = is_animated(data)
    # 只发静态的开关：动图取首帧
    if animated and str(db.get_settings().get("ai_send_animated", "1")) != "1":
        png = to_png(data, max_side=480)
        if not png:
            return False
        data = png
        animated = False

    try:
        onebot.send_message(seg_type, target_id,
                            [{"type": "image", "data": {"file": "base64://" +
                              base64.b64encode(data).decode()}}])
        log.info("从 QQ 收藏表情发了一张（%s，%d 字节）",
                 "动图" if animated else "静态", len(data))
        return True
    except Exception as e:
        log.warning("发 QQ 收藏表情失败: %s", e)
        return False

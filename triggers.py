"""@关键词触发回复：接收 OneBot 消息事件，匹配触发词后回复。"""
from __future__ import annotations

import datetime as _dt
import threading
import time
from zoneinfo import ZoneInfo

import db
import onebot
import sender

TZ = ZoneInfo("Asia/Shanghai")

# (trigger_id, session_key) -> 上次回复时间戳
_last_hit: dict[tuple[int, str], float] = {}
_lock = threading.Lock()
_self_id: int | None = None
_self_id_at: float = 0.0


def _self_qq() -> int | None:
    """缓存自己的 QQ 号（10 分钟内不重复查询）。"""
    global _self_id, _self_id_at
    now = time.time()
    if _self_id is not None and now - _self_id_at < 600:
        return _self_id
    try:
        info = onebot.get_login_info()
        _self_id = int(info.get("user_id") or 0) or None
        _self_id_at = now
    except Exception:
        pass
    return _self_id


def _extract_text(segments) -> str:
    parts = []
    if isinstance(segments, str):
        return segments
    for seg in segments or []:
        if seg.get("type") == "text":
            parts.append(seg.get("data", {}).get("text", ""))
    return "".join(parts)


def _at_list(segments) -> list[str]:
    out = []
    if isinstance(segments, list):
        for seg in segments:
            if seg.get("type") == "at":
                out.append(str(seg.get("data", {}).get("qq", "")))
    return out


def _scope_ok(trigger: dict, is_group: bool, target_id: int) -> bool:
    scope = trigger.get("scope") or "all"
    if scope == "all":
        return True
    if scope == "group" and is_group:
        return True
    if scope == "private" and not is_group:
        return True
    if scope.startswith("group:"):
        ids = {s.strip() for s in scope[6:].split(",") if s.strip()}
        return is_group and str(target_id) in ids
    return False


def handle_event(event: dict) -> None:
    """处理一条 OneBot 消息事件（同步调用，放线程里跑）。"""
    if event.get("message_type") not in ("group", "private"):
        return
    segments = event.get("message")
    text = _extract_text(segments).strip()
    if not text:
        return
    is_group = event.get("message_type") == "group"
    target_id = int(event.get("group_id") or event.get("user_id") or 0)
    session_key = f"{'g' if is_group else 'p'}:{target_id}"
    at_ids = _at_list(segments)
    self_qq = _self_qq()
    mentioned = str(self_qq) in at_ids or "all" in at_ids

    reply_text = None
    trigger = None
    for t in db.list_triggers():
        if not t.get("enabled"):
            continue
        kw = (t.get("keyword") or "").strip()
        if not kw:
            continue
        # 关键词匹配：正文里出现即命中；at_required 时还必须 @机器人
        if kw.lower() not in text.lower():
            continue
        if t.get("at_required") and not mentioned:
            continue
        if not _scope_ok(t, is_group, target_id):
            continue
        # 冷却
        cd = int(t.get("cooldown") or 0)
        if cd > 0:
            key = (t["id"], session_key)
            with _lock:
                last = _last_hit.get(key, 0.0)
                if time.time() - last < cd:
                    continue
                _last_hit[key] = time.time()
        trigger = t
        break

    if not trigger:
        return

    now = _dt.datetime.now(TZ)
    task_like = {
        "name": trigger.get("keyword"),
        "content": trigger.get("reply_text") or "",
        "vars": [],
    }
    try:
        rendered = sender.render_template(task_like["content"], task_like, now)
    except Exception as e:
        db.add_log(f"触发词《{trigger['keyword']}》回复渲染失败：{e}", "error", None)
        return

    # 随机名言模式：replace=整条回复只发名言；append=正文后另起一段附名言
    qmode = trigger.get("quote_mode") or "off"
    if qmode == "replace":
        rendered = sender.random_quote()
    elif qmode == "append":
        quote = sender.random_quote()
        rendered = f"{rendered}\n{quote}" if rendered.strip() else quote

    segments_out = []
    # 图片放最前（防折叠），随后 @ 触发者，最后正文
    try:
        img = sender.pick_image(trigger)
    except RuntimeError:
        img = None
    if img:
        segments_out.append(onebot.image_segment(img))
    # 是否 @ 触发者：at_reply = 0 不带、1 带（默认）；私聊无需 @
    at_reply = trigger.get("at_reply")
    at_reply = True if at_reply is None else bool(int(at_reply))
    if is_group and self_qq and at_reply:
        uid = event.get("sender", {}).get("user_id")
        if uid:
            segments_out.append({"type": "at", "data": {"qq": str(uid)}})
            segments_out.append(onebot.text_segment(" "))
    if rendered.strip():
        segments_out.append(onebot.text_segment(rendered.strip()))

    if not segments_out:
        db.add_log(f"触发词《{trigger['keyword']}》命中但没有回复内容", "warn", None)
        return

    try:
        onebot.send_message("group" if is_group else "private", target_id, segments_out)
        db.add_log(f"触发词《{trigger['keyword']}》已回复 {'群' if is_group else '私聊'} {target_id}", "info", None)
    except Exception as e:
        # 带 @ 的回复失败（如提问者已退群、uid 解析失败）时降级为不带 @ 再发一次
        if any(sg.get("type") == "at" for sg in segments_out):
            try:
                retry = [sg for sg in segments_out if sg.get("type") != "at"]
                retry = [sg for i, sg in enumerate(retry) if not (sg.get("type") == "text" and sg["data"]["text"] == " " and i == 0)]
                onebot.send_message("group" if is_group else "private", target_id, retry)
                db.add_log(f"触发词《{trigger['keyword']}》已回复 {'群' if is_group else '私聊'} {target_id}（@提问者失败已降级）", "info", None)
                return
            except Exception:
                pass
        db.add_log(f"触发词《{trigger['keyword']}》回复失败：{e}", "error", None)


def handle_event_async(event: dict) -> None:
    threading.Thread(target=handle_event, args=(event,), daemon=True).start()

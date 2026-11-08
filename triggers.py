"""@关键词触发回复：接收 OneBot 消息事件，匹配触发词后回复。"""
from __future__ import annotations

import datetime as _dt
import queue
import re
import threading
import time
from zoneinfo import ZoneInfo

import db
import onebot
import sender
import ai
import stickers

TZ = ZoneInfo("Asia/Shanghai")

# (trigger_id, session_key) -> 上次回复时间戳
_last_hit: dict[tuple[int, str], float] = {}
_lock = threading.Lock()
_self_id: int | None = None
_self_id_at: float = 0.0
# 群号 -> 上次 AI 回复时间（all 模式限流，别刷屏）
_last_ai_group: dict[int, float] = {}
# 斗图节奏：群号 -> 上次发/收表情的时间、连续张数
_last_sticker_at: dict[int, float] = {}
_sticker_streak: dict[int, int] = {}
# 媒体回复限速：群号 -> 上次因为图片而回复的时间
_last_media_reply: dict[int, float] = {}
# 斗图节奏限速：群号 -> 上次斗图回复的时间（跟着对方节奏走，间隔很短）
_last_fight_reply: dict[int, float] = {}
# 偷图限速：群号 -> 上次尝试收藏的时间（收藏要额外调一次模型，别太频繁）
_last_collect_try: dict[int, float] = {}


def _self_qq(event: dict | None = None) -> int | None:
    """自己的 QQ 号。优先用事件里自带的 self_id（零成本），其次用缓存。"""
    global _self_id, _self_id_at
    # 事件里一般就带着 self_id，直接用，省一次 HTTP
    if event:
        sid = event.get("self_id")
        if sid:
            try:
                _self_id = int(sid)
                _self_id_at = time.time()
                return _self_id
            except (TypeError, ValueError):
                pass

    now = time.time()
    if _self_id is not None and now - _self_id_at < 3600:
        return _self_id
    try:
        info = onebot.get_login_info()
        _self_id = int(info.get("user_id") or 0) or None
        _self_id_at = now
    except Exception:
        pass
    return _self_id


def _extract_text(segments) -> str:
    """兼容 array 与 CQ 码 string 两种 messagePostFormat。"""
    parts = []
    if isinstance(segments, str):
        # CQ 码形式：去掉 [CQ:...]，保留纯文字
        return _CQ_RE.sub("", segments)
    for seg in segments or []:
        if not isinstance(seg, dict):
            continue
        if seg.get("type") == "text":
            parts.append((seg.get("data") or {}).get("text", ""))
    return "".join(parts)


def _at_list(segments) -> list[str]:
    out = []
    if isinstance(segments, str):
        for m in _CQ_AT_RE.finditer(segments):
            out.append(m.group(1))
        return out
    if isinstance(segments, list):
        for seg in segments:
            if not isinstance(seg, dict):
                continue
            if seg.get("type") == "at":
                out.append(str((seg.get("data") or {}).get("qq", "")))
    return out


_CQ_RE = re.compile(r"\[CQ:[^\]]*\]")
_CQ_AT_RE = re.compile(r"\[CQ:at,qq=([^,\]]+)")


def _is_message_event(event: dict) -> bool:
    """有些实现 message_type 缺失，用 post_type 兜底判断。"""
    if event.get("message_type") in ("group", "private"):
        return True
    key = "group_id" if event.get("group_id") else ("user_id" if event.get("user_id") else None)
    return event.get("post_type") == "message" and key is not None


def _event_target(event: dict) -> tuple[bool, int]:
    """返回 (是否群聊, 目标id)。"""
    is_group = event.get("message_type") == "group" or bool(event.get("group_id"))
    target_id = int(event.get("group_id") or event.get("user_id") or 0)
    return is_group, target_id


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


def _handle_media(event: dict, img_segs: list[dict], text: str, is_group: bool,
                  target_id: int, mentioned: bool) -> bool:
    """处理图片/表情/小黄脸：看图 → 判断存不存 → 一定给出反应。

    原则：带媒体的消息绝不静默丢弃。看不懂也要接一句，或者回一张表情。
    """
    cfg = db.get_settings()
    seg = img_segs[0]
    is_face = seg.get("type") == "face"
    like_sticker = (not is_face) and stickers.is_sticker_like(seg)
    snd = event.get("sender", {}) or {}
    who = snd.get("card") or snd.get("nickname") or "有人"

    # 小黄脸：直接读它自带的表情名（不用调视觉模型）
    face_name = ""
    if is_face:
        d = seg.get("data") or {}
        face_name = str(d.get("raw", {}).get("faceText") or "") if isinstance(d.get("raw"), dict) else ""
        if not face_name:
            face_name = str(d.get("summary") or d.get("id") or "")

    # 1) 斗图节奏 + 限速
    streak = 1
    allow_reply = True
    if is_group:
        now_t = time.time()
        with _lock:
            recent = _last_sticker_at.get(int(target_id), 0.0)
            prev = _sticker_streak.get(int(target_id), 0)
            streak = prev + 1 if now_t - recent <= 120 else 1
            _sticker_streak[int(target_id)] = streak
            _last_sticker_at[int(target_id)] = now_t

            if streak >= 2:
                # 斗图：节奏跟着对方走，只要不是自己刚甩完立刻又接
                last_fight = _last_fight_reply.get(int(target_id), 0.0)
                allow_reply = (now_t - last_fight) >= 12
                if allow_reply:
                    _last_fight_reply[int(target_id)] = now_t
            else:
                # 普通群图：90 秒才主动接一次，免得每张都插嘴
                last_media = _last_media_reply.get(int(target_id), 0.0)
                allow_reply = (now_t - last_media) >= 90
                if allow_reply:
                    _last_media_reply[int(target_id)] = now_t

    # 被 @ 或私聊时不受限速影响；普通群图受
    if is_group and not mentioned and not allow_reply:
        return True

    fighting = is_group and streak >= 2

    # 2) 看图（小黄脸不调视觉，省额度）
    desc = ""
    need_look = (not is_face) and (
        mentioned or bool(text) or (is_group and streak >= 2) or not is_group
    )
    if need_look:
        try:
            desc = stickers.describe(seg, hint=text)
        except Exception as e:
            db.add_log(f"看图失败：{e}", "warn", None)

    if desc:
        db.add_log(f"看懂了 {who} 的图：{desc[:60]}", "info", None)
        if is_group:
            remember_line(target_id, f"{who}：[图片]{desc[:40]}", _self_qq(event))

    # 3) 偷图（只存图片表情，小黄脸不存）
    #    收藏要多调一次模型，限速：同一群 2 分钟最多试一次
    if like_sticker and desc and str(cfg.get("ai_save_sticker", "1")) == "1":
        now_c = time.time()
        with _lock:
            last_try = _last_collect_try.get(int(target_id), 0.0)
            allow = (now_c - last_try) >= 120
            if allow:
                _last_collect_try[int(target_id)] = now_c
        if allow:
            _maybe_save(desc, seg)

    session_key = f"g:{target_id}" if is_group else f"p:{target_id}"
    what = "小黄脸表情" if is_face else "图片"

    # 4) 私聊：一定回
    if not is_group:
        if is_face:
            prompt = f"{who}发来一个{what}（{face_name or '没名字'}）"
        elif desc:
            prompt = f"{who}发来一张图：{desc}"
        else:
            prompt = f"{who}发来一张图（没看清具体内容）"
        if text:
            prompt += f"\n他说：{text}"
        prompt += "\n就着这个回一句（短，别客套，别抱怨看不清）。想回张表情就写 [sticker]。"
        try:
            reply = ai.chat(prompt, session_key=session_key)
        except Exception as e:
            db.add_log(f"媒体回话失败：{e}", "warn", None)
            reply = None
        if reply:
            _send_ai_reply(reply, False, target_id, event)
        else:
            # 模型不可用也要有反应：回一张表情
            if str(cfg.get("ai_use_qq_fav", "1")) == "1" and stickers.send_qq_favorite("private", target_id):
                pass
            else:
                st = stickers.random_sticker()
                if st:
                    stickers.send_sticker("private", target_id, st)
        return True

    # 5) 群聊：被 @ / 在斗图 → 甩表情
    should_fight = mentioned or fighting
    if should_fight:
        # 优先用自己 QQ 收藏的表情（更贴自己的口味），没有再退回本地库
        sent_img = False
        if str(cfg.get("ai_use_qq_fav", "1")) == "1":
            sent_img = stickers.send_qq_favorite("group", target_id)
            if sent_img:
                db.add_log("斗图：发了一张 QQ 收藏表情", "info", None)
        if not sent_img:
            st = stickers.random_sticker()
            sent_img = bool(st and stickers.send_sticker("group", target_id, st))
            if sent_img:
                db.add_log(f"斗图：甩出 #{st['id']}（{st.get('note') or '无备注'}）", "info", None)
        # 配一句嘴
        try:
            ctx = f"{who}发了张图：{desc}" if desc else f"{who}发了{'小黄脸' if is_face else '张图'}"
            line = ai.chat(
                ctx + "\n你刚回了张表情。补一句很短的话（3~10 字），挑衅/吐槽/看戏都行，别客套。",
                session_key=session_key)
            if line:
                t, _a = _parse_actions(line)
                if t.strip():
                    onebot.send_message("group", target_id, [onebot.text_segment(t.strip())])
        except Exception:
            pass
        return True

    # 6) 普通群聊图片：让模型决定接不接；模型不可用时按概率甩张表情
    if is_face and not text:
        # 群里随手发小黄脸、没 @ 你：多数情况不接，免得吵
        if streak < 3:
            return True

    if desc or text or is_face:
        ctx = (f"群里{who}发了张图：{desc}" if desc
               else f"群里{who}发了个{'小黄脸' if is_face else '图'}")
        if text:
            ctx += f"\n配文：{text}"
        prompt = ctx + "\n要不要接话？接就着这个说一句（短，别客套），想回表情写 [sticker]，不想接只回 NO。"
        try:
            reply = ai.chat(prompt, session_key=session_key)
        except Exception as e:
            db.add_log(f"媒体接话失败：{e}", "warn", None)
            reply = None
        if reply and not reply.strip().upper().startswith("NO"):
            _send_ai_reply(reply, True, target_id, event)
        return True

    # 没看懂又没配文：甩张表情，别沉默
    if str(cfg.get("ai_use_qq_fav", "1")) == "1" and stickers.send_qq_favorite("group", target_id):
        return True
    st = stickers.random_sticker()
    if st:
        stickers.send_sticker("group", target_id, st)
    return True


def _maybe_save(desc: str, seg: dict) -> int | None:
    """问模型这张图值不值得偷；值得就存，并让它自己写备注。返回新 id。

    去重按图片内容（md5），不是按备注——同一张图换个说法也不能重复存。
    """
    # 先算内容哈希：已经在库里就别白调模型了
    try:
        if stickers.already_saved(seg):
            return None
    except Exception:
        pass

    try:
        reply = ai.chat(
            f"这张图：{desc}\n"
            "判断它是不是值得收藏的表情包。值得就输出：SAVE|备注|标签\n"
            "备注写这张图什么意思（20 字内），标签用逗号分隔（如 嘲讽,无语）。\n"
            "普通照片/截图/风景/人像/聊天记录，就只回 SKIP。",
            session_key="sticker-curator")
    except Exception as e:
        db.add_log(f"判断表情失败：{e}", "warn", None)
        return None
    if not reply or "SAVE" not in reply.upper():
        return None
    m = _SAVE_RE.search(reply)
    note = (m.group(1).strip() if m else desc[:20])
    tags = (m.group(2).strip() if m else "")
    if not note:
        return None
    # 内容去重由 db.add_sticker 负责（比对 md5）
    sid = stickers.save_sticker(seg, note=note, tags=tags)
    if not sid:
        return None
    db.add_log(f"偷了张表情 #{sid}：{note}", "info", None)

    # 同时收藏进 QQ 的「收藏表情」，手机上也能直接用
    if str(db.get_settings().get("ai_fav_to_qq", "1")) == "1":
        try:
            sid2 = db.get_sticker(sid)
            if sid2 and stickers.favorite_to_qq(sid2):
                db.add_log(f"已同步收藏到 QQ 表情：{note}", "info", None)
        except Exception as e:
            db.add_log(f"同步到 QQ 收藏失败：{e}", "warn", None)
    return sid


_SAVE_RE = re.compile(r"SAVE\s*\|\s*([^|\n]{1,40})\s*\|\s*([^\n]{0,60})", re.IGNORECASE)


def _send_ai_reply(reply: str, is_group: bool, target_id: int, event: dict) -> bool:
    """发送一条 AI 回复（支持 [face:x] / [poke] / [sticker] 标记）。"""
    clean_text, actions = _parse_actions(reply)
    delivered = False
    if clean_text and clean_text.strip() and "[taunt]" not in clean_text:
        segs = [onebot.text_segment(clean_text.strip())]
        try:
            onebot.send_message("group" if is_group else "private", target_id, segs)
            delivered = True
        except Exception as e:
            db.add_log(f"发消息失败：{e}", "warn", None)
    for kind, arg in actions:
        try:
            if kind == "face":
                onebot.send_message("group" if is_group else "private", target_id,
                                    [{"type": "face", "data": {"id": str(arg)}}])
                delivered = True
            elif kind == "poke" and is_group:
                uid = (event.get("sender") or {}).get("user_id")
                if uid:
                    onebot.send_poke(int(target_id), int(uid))
                    delivered = True
            elif kind == "sticker":
                where = "group" if is_group else "private"
                if str(db.get_settings().get("ai_use_qq_fav", "1")) == "1" \
                        and stickers.send_qq_favorite(where, target_id):
                    delivered = True
                else:
                    st = stickers.random_sticker()
                    if st:
                        delivered = stickers.send_sticker(where, target_id, st) or delivered
        except Exception as e:
            db.add_log(f"动作 {kind} 失败：{e}", "warn", None)
    return delivered


def handle_event(event: dict) -> None:
    """处理一条 OneBot 消息事件（同步调用，放线程里跑）。"""
    if not _is_message_event(event):
        return
    segments = event.get("message")
    text = _extract_text(segments).strip()
    is_group, target_id = _event_target(event)
    session_key = f"{'g' if is_group else 'p'}:{target_id}"
    at_ids = _at_list(segments)
    self_qq = _self_qq(event)
    mentioned = str(self_qq) in at_ids or "all" in at_ids

    # 只有 @ 没有文字（只 @ 了一下）：也当一句空话交给 AI，让它有反应
    img_segs = [s for s in (segments or [])
                if isinstance(s, dict) and s.get("type") in ("image", "mface")]
    face_segs = [s for s in (segments or [])
                 if isinstance(s, dict) and s.get("type") == "face"]
    has_media = isinstance(segments, list) and any(
        isinstance(s, dict) and s.get("type") in ("image", "face", "mface", "record") for s in segments)

    # 任何带媒体（图片/表情包/小黄脸）的消息，都走统一处理并且一定给出反应，绝不丢弃。
    if (img_segs or face_segs) and self_qq and str(event.get("user_id")) != str(self_qq):
        try:
            _handle_media(event, img_segs or face_segs, text, is_group, target_id, mentioned)
        except Exception as e:
            db.add_log(f"处理媒体消息出错：{e}", "error", None)
        return

    if not text:
        if mentioned:
            text = "（@了你，没说话）"
        else:
            db.add_log(
                "收到纯语音消息，已忽略："
                + str(event.get("raw_message") or segments)[:120], "warn", None)
            return

    # 记住群里每句话（不只是 @ 的），这样「只@不说话」时能知道大家在聊什么
    if is_group:
        snd0 = event.get("sender", {}) or {}
        who0 = snd0.get("card") or snd0.get("nickname") or str(snd0.get("user_id") or "")
        if text and not text.startswith("（@了你"):
            remember_line(target_id, f"{who0}：{text}" if who0 else text, self_qq)

    # 自己的话不进上下文（否则会自问自答）
    if self_qq and str(event.get("user_id")) == str(self_qq):
        return

    # 旁听：不管回不回，都把这句记进会话上下文，被叫到时才能接上前文
    if is_group and text and not text.startswith("（@了你"):
        snd0 = event.get("sender", {}) or {}
        who0 = snd0.get("card") or snd0.get("nickname") or ""
        ai.observe(session_key, f"{who0}：{text}" if who0 else text)

    # 限速：只限制「主动插话」，被 @ / 被点名时永远优先回，不受限速影响
    if is_group and not mentioned and ai.reply_mode() == "all":
        try:
            gap = int(db.get_settings().get("ai_group_gap") or 25)
        except Exception:
            gap = 25
        now_t = time.time()
        with _lock:
            last = _last_ai_group.get(int(target_id), 0.0)
            if gap > 0 and now_t - last < gap:
                db.add_log(
                    f"群 {target_id} 限速中（距上次回复 {now_t - last:.0f}s < {gap}s），这条不接",
                    "info", None)
                return
            _last_ai_group[int(target_id)] = now_t

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
        # 没命中任何关键词触发词：尝试 AI 聊天回复
        if _try_ai_reply(event, text, is_group, target_id, mentioned):
            return
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


_RECENT: dict[int, list[str]] = {}
_RECENT_LOCK = threading.Lock()
_RECENT_MAX = 12


def remember_line(group_id: int, line: str, self_id: int | None = None) -> None:
    """把群里的每句话记进环形缓冲，供「只@不说话」时取上下文。"""
    if not line or not group_id:
        return
    with _RECENT_LOCK:
        buf = _RECENT.setdefault(int(group_id), [])
        buf.append(line[:80])
        if len(buf) > _RECENT_MAX:
            del buf[: len(buf) - _RECENT_MAX]


def _recent_group_lines(group_id: int, limit: int = 6) -> list[str]:
    with _RECENT_LOCK:
        buf = list(_RECENT.get(int(group_id), []))
    return buf[-limit:]


# 模型可以用这些标记做动作：
#   [face:13] 发小黄脸   [poke] 戳对方   [sticker] 从表情库甩一张
_ACTION_RE = re.compile(r"\[(face|poke|sticker)(?::([0-9]{1,3}))?\]", re.IGNORECASE)


def _parse_actions(reply: str) -> tuple[str, list[tuple[str, str]]]:
    """把回复里的 [face:13] / [poke] / [sticker] 拆出来，返回 (纯文字, 动作列表)。"""
    actions: list[tuple[str, str]] = []
    cfg = db.get_settings()
    if str(cfg.get("ai_use_sticker", "1")) != "1":
        reply = re.sub(r"\[sticker\]", "", reply, flags=re.IGNORECASE)
    for m in _ACTION_RE.finditer(reply):
        kind = m.group(1).lower()
        arg = m.group(2) or ("" if kind in ("poke", "sticker") else "178")
        if kind == "face" and str(cfg.get("ai_use_face", "1")) != "1":
            continue
        if kind == "poke" and str(cfg.get("ai_use_poke", "1")) != "1":
            continue
        if kind == "sticker" and str(cfg.get("ai_use_sticker", "1")) != "1":
            continue
        actions.append((kind, arg))
    text = _ACTION_RE.sub("", reply).strip()
    # 动作最多两个，别刷屏
    return text, actions[:2]


def handle_poke(event: dict) -> None:
    """有人戳了机器人：按设置回戳，或者回一句话。"""
    if event.get("target_id") and int(event["target_id"]) != int(_self_qq(event) or 0):
        return  # 戳的不是我
    settings = db.get_settings()
    if str(settings.get("ai_poke_reply_poke", "1")) == "1":
        group_id = event.get("group_id")
        uid = event.get("user_id")
        try:
            if group_id and uid:
                onebot.send_poke(int(group_id), int(uid))
            elif uid:
                onebot.send_poke(None, int(uid))
            db.add_log(f"被戳了，回戳 {uid}", "info", None)
            return
        except Exception as e:
            db.add_log(f"回戳失败：{e}", "warn", None)

    # 回戳失败或不回戳：让 AI 说句话
    try:
        reply = ai.chat("（有人戳了戳你，随口应一句）",
                        session_key=f"p:{event.get('user_id')}", speaker="")
        if reply:
            text, actions = _parse_actions(reply)
            if text:
                onebot.send_message("private", int(event["user_id"]),
                                    [onebot.text_segment(text)])
                db.add_log(f"被戳了，回了句话给 {event.get('user_id')}", "info", None)
    except Exception as e:
        db.add_log(f"戳一戳回复失败：{e}", "warn", None)


def _try_ai_reply(event: dict, text: str, is_group: bool, target_id: int, mentioned: bool) -> bool:
    """未命中触发词时，根据 AI 回复模式决定是否用大模型回复。"""
    mode = ai.reply_mode()
    if mode == "off":
        return False
    # all = 群里任何话都接；at = 只接 @ 的
    if is_group and mode == "at" and not mentioned:
        return False

    self_qq = _self_qq(event)
    session_key = f"g:{target_id}" if is_group else f"p:{target_id}"
    snd = event.get("sender", {}) or {}
    speaker = snd.get("card") or snd.get("nickname") or ""

    # 去掉 CQ @ 码，保留正文
    qtext = text
    if self_qq:
        qtext = qtext.replace(f"[CQ:at,qq={self_qq}]", " ")
    qtext = qtext.strip()

    # 上下文已经在 ai 的会话记忆里（triggers 会旁听每句话），这里只描述"这次发生了什么"
    if is_group and (not qtext or qtext in ("（@了你，没说话）",)):
        qtext = "（有人 @ 了你但没打字，接着群里刚才的话题随口应一句）"
    elif is_group and mentioned:
        qtext = f"（有人 @ 你：{qtext}）" if qtext else "（有人 @ 了你）"

    try:
        reply = ai.chat(qtext, session_key=session_key, speaker=speaker)
    except Exception as e:
        db.add_log(f"AI 聊天出错：{e}", "error", None)
        return False
    if not reply:
        return False

    # 解析模型输出里的控制标记：[face:xx] 发表情，[poke] 戳对方
    clean_text, actions = _parse_actions(reply)

    # 被 @ 或私聊时：只有表情没有文字，说明模型偷懒了，再要一句
    if mentioned and not clean_text.strip() and actions:
        try:
            extra = ai.chat("（再说一句，3~12 字，别只发表情）", session_key=session_key)
            if extra:
                t2, _a2 = _parse_actions(extra)
                if t2.strip():
                    clean_text = t2.strip()
        except Exception:
            pass

    delivered = False

    # 1) 先发文字（不带 @，默认）
    if clean_text and clean_text.strip():
        at_reply = str(db.get_settings().get("ai_at_reply", "0")) == "1"
        segs = []
        if is_group and at_reply and self_qq and snd.get("user_id"):
            segs.append({"type": "at", "data": {"qq": str(snd["user_id"])}})
            segs.append(onebot.text_segment(" "))
        segs.append(onebot.text_segment(clean_text))
        try:
            onebot.send_message("group" if is_group else "private", target_id, segs)
            delivered = True
        except Exception as e:
            if at_reply and len(segs) > 1:
                try:
                    onebot.send_message("group" if is_group else "private", target_id,
                                        [onebot.text_segment(clean_text)])
                    delivered = True
                    db.add_log("AI 回复已发出（@ 失败，降级为纯文本）", "info", None)
                except Exception as e2:
                    db.add_log(f"AI 回复失败：{e2}", "error", None)
            else:
                db.add_log(f"AI 回复失败：{e}", "error", None)

    # 2) 再发动作：表情 / 戳一戳 / 表情包
    for kind, arg in actions:
        try:
            if kind == "face":
                onebot.send_message("group" if is_group else "private", target_id,
                                    [{"type": "face", "data": {"id": str(arg)}}])
                delivered = True
            elif kind == "poke" and is_group and snd.get("user_id"):
                onebot.send_poke(int(target_id), int(snd["user_id"]))
                delivered = True
            elif kind == "sticker":
                where = "group" if is_group else "private"
                if str(db.get_settings().get("ai_use_qq_fav", "1")) == "1" \
                        and stickers.send_qq_favorite(where, target_id):
                    delivered = True
                else:
                    st = stickers.random_sticker()
                    if st:
                        delivered = stickers.send_sticker(where, target_id, st) or delivered
        except Exception as e:
            db.add_log(f"AI 动作 {kind} 执行失败：{e}", "warn", None)

    if delivered:
        db.add_log(f"AI 已回复 {'群' if is_group else '私聊'} {target_id}"
                   f"（模式={mode}，动作={[a for a, _ in actions] or '无'}）", "info", None)
    return delivered


# ---------------- 消息处理队列 ----------------
# 群里刷屏时，原来每条消息各开一个线程，会并发打模型接口：
# 既容易触发限流，回复顺序也会乱（后发的先回）。
# 改成每个会话一条队列、串行处理。
#
# 关键设计：刷屏时"合并"而不是"逐条排队"——
# 处理一条消息要 2~5 秒，若逐条排队，10 条消息会拖 30 秒以上，全都变成过期回复。
# 所以：普通消息只保留最新一条（旧的在队列里等着时被新消息替换），
# 而被 @ 的消息一条都不合并、不丢弃。
_QUEUE_MAX = 6
_PENDING: dict[str, dict] = {}          # 会话 -> 正在排队的最新普通消息
_QUEUE_LOCK = threading.Lock()


def _worker(session_key: str) -> None:
    """每个会话一个常驻 worker，串行处理。"""
    import queue as _q
    q = _q.Queue()
    _QUEUES[session_key] = q
    while True:
        event = q.get()
        try:
            handle_event(event)
        except Exception as e:
            db.add_log(f"处理消息出错（{session_key}）：{e}", "error", None)
        finally:
            q.task_done()
            with _QUEUE_LOCK:
                # 处理完这条，把期间攒下的"最新普通消息"补进来
                nxt = _PENDING.pop(session_key, None)
            if nxt is not None:
                q.put_nowait(nxt)


_QUEUES: dict[str, "queue.Queue"] = {}


def _enqueue(session_key: str, event: dict, mentioned: bool) -> None:
    with _QUEUE_LOCK:
        q = _QUEUES.get(session_key)
        if q is None:
            threading.Thread(target=_worker, args=(session_key,), daemon=True).start()
            # worker 线程里会建队列；这里等它建好
            for _ in range(50):
                if session_key in _QUEUES:
                    break
                time.sleep(0.01)
            q = _QUEUES.get(session_key)
            if q is None:
                return

        if not q.empty() or session_key in _PENDING:
            # 正忙：@ 消息单独排队（不丢），普通消息只留最新一条
            if mentioned:
                q.put_nowait(event)
                db.add_log(f"忙碌中，@ 消息已排队（{session_key}）", "info", None)
            else:
                _PENDING[session_key] = event       # 旧的直接被覆盖
            return

        q.put_nowait(event)


def handle_event_async(event: dict) -> None:
    """按会话串行处理，避免并发导致乱序和丢消息。"""
    segments = event.get("message")
    at_ids = _at_list(segments)
    self_qq = _self_qq(event)
    mentioned = bool(self_qq) and (str(self_qq) in at_ids or "all" in at_ids)
    is_group, target_id = _event_target(event)
    session_key = f"{'g' if is_group else 'p'}:{target_id}"
    _enqueue(session_key, event, mentioned)


def handle_poke_async(event: dict) -> None:
    threading.Thread(target=handle_poke, args=(event,), daemon=True).start()

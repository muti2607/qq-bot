"""AI 聊天：多接口自动故障转移 + 会话短期记忆。

支持任意兼容 OpenAI /v1/chat/completions 的服务，并在多个接口间自动切换：
配置了多个接口时，按顺序尝试；某个接口报错/超时/返回空，立刻换下一个。

配置方式（后台“设置 → AI 聊天”）：
  ai_providers 每行一个接口，格式：
      名称|接口地址|模型名|API Key
  例如：
      阶跃|http://172.18.0.1:18999/v1|stepfun-ai/step-5-preview|ofm-xxxx
      OpenRouter|http://172.18.0.1:18999/v1|openrouter/free|ofm-xxxx
      智谱|https://open.bigmodel.cn/api/paas/v4|glm-4-flash|xxxxxxxx
      DeepSeek|https://api.deepseek.com/v1|deepseek-chat|sk-xxxxxxxx

  若 ai_providers 留空，则回退使用 ai_api_base / ai_model / ai_api_key 的单接口配置。

  额外内置一个**无需 Key 的免费兜底接口**（Pollinations），
  由 ai_keyless_fallback 控制是否启用，默认开启。

会话记忆：按 session_key（群/私聊）保留最近几轮对话，让回复能接上上文；
超过 TTL 或轮数自动淘汰，不落盘。
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Any

import db
import httpx

log = logging.getLogger("qqbot.ai")

TIMEOUT = 30.0
TEST_PROMPT = "用一句简短的中文跟我打个招呼"

# 会话记忆：session_key -> {"at": ts, "msgs": [(role, content), ...]}
_HISTORY: dict[str, dict[str, Any]] = {}
_HISTORY_LOCK = threading.Lock()
HISTORY_TURNS = 6          # 最多保留 6 条（3 轮问答）
HISTORY_TTL = 1800         # 30 分钟没用就丢弃
HISTORY_MAX_SESSIONS = 300

# 记住上次成功的接口名，下次优先尝试它
_last_good: str | None = None
# 上次成功的视觉模型
_last_vision: str | None = None
# 视觉调用的全局限速：免费额度很小，连续看图会被 429
_vision_lock = threading.Lock()
_vision_last: float = 0.0
VISION_MIN_GAP = 8.0      # 两次看图之间至少隔这么久
_vision_cooldown_until: float = 0.0
VISION_COOLDOWN = 300.0   # 连续失败后冷静 5 分钟

# 内置无 Key 兜底接口（OpenAI 兼容）
KEYLESS_PROVIDERS: list[dict[str, str]] = [
    {"name": "Pollinations(免Key)", "base": "https://text.pollinations.ai/openai", "model": "openai", "key": ""},
]


def _conf() -> dict[str, str]:
    s = db.get_settings()
    return {
        "enabled": str(s.get("ai_enabled", "0")),
        "api_base": (s.get("ai_api_base") or "").rstrip("/"),
        "api_key": s.get("ai_api_key") or "",
        "model": s.get("ai_model") or "",
        "system_prompt": s.get("ai_system_prompt") or "",
        "reply_mode": str(s.get("ai_reply_mode") or "off"),
        "providers": s.get("ai_providers") or "",
        "keyless": str(s.get("ai_keyless_fallback", "1")),
    }


def is_enabled() -> bool:
    return _conf()["enabled"] == "1"


def reply_mode() -> str:
    return _conf()["reply_mode"]


def _parse_providers(raw: str) -> list[dict[str, str]]:
    """解析多接口配置。支持两种写法：
    1) 每行 `名称|地址|模型|Key`
    2) JSON 数组 [{"name":..,"base":..,"model":..,"key":..}]
    """
    raw = (raw or "").strip()
    if not raw:
        return []
    out: list[dict[str, str]] = []

    if raw.startswith("["):
        try:
            data = json.loads(raw)
            for it in data:
                if not isinstance(it, dict):
                    continue
                base = str(it.get("base") or "").strip().rstrip("/")
                model = str(it.get("model") or "").strip()
                if base and model:
                    out.append({
                        "name": str(it.get("name") or base)[:40],
                        "base": base,
                        "model": model,
                        "key": str(it.get("key") or "").strip(),
                    })
            return out
        except json.JSONDecodeError:
            pass

    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in re.split(r"\s*\|\s*", line)]
        if len(parts) < 3:
            continue
        name, base, model = parts[0], parts[1], parts[2]
        key = parts[3] if len(parts) > 3 else ""
        base = base.rstrip("/")
        if base and model:
            out.append({"name": name[:40] or base, "base": base, "model": model, "key": key})
    return out


def providers() -> list[dict[str, str]]:
    """按优先级返回可用接口列表。"""
    c = _conf()
    items = _parse_providers(c["providers"])

    # 兼容旧的单接口配置
    if not items and c["api_base"] and c["model"]:
        items.append({
            "name": "默认接口",
            "base": c["api_base"],
            "model": c["model"],
            "key": c["api_key"],
        })

    if c["keyless"] == "1":
        items.extend([dict(p) for p in KEYLESS_PROVIDERS])

    # 上次成功的放最前
    if _last_good:
        items.sort(key=lambda p: 0 if p["name"] == _last_good else 1)
    return items


# ---------------- 会话记忆 ----------------


def _history_get(session_key: str) -> list[dict[str, str]]:
    if not session_key:
        return []
    now = time.time()
    with _HISTORY_LOCK:
        item = _HISTORY.get(session_key)
        if not item:
            return []
        if now - item["at"] > HISTORY_TTL:
            _HISTORY.pop(session_key, None)
            return []
        return list(item["msgs"])


def _history_add(session_key: str, user_text: str, reply: str) -> None:
    if not session_key:
        return
    now = time.time()
    with _HISTORY_LOCK:
        item = _HISTORY.get(session_key)
        if not item or now - item["at"] > HISTORY_TTL:
            item = {"at": now, "msgs": []}
            _HISTORY[session_key] = item
        item["at"] = now
        item["msgs"].append({"role": "user", "content": user_text})
        item["msgs"].append({"role": "assistant", "content": reply})
        if len(item["msgs"]) > HISTORY_TURNS * 2:
            item["msgs"] = item["msgs"][-HISTORY_TURNS * 2:]
        # 会话数过多时清掉最旧的一批
        if len(_HISTORY) > HISTORY_MAX_SESSIONS:
            ordered = sorted(_HISTORY.items(), key=lambda kv: kv[1]["at"])
            for k, _ in ordered[: len(_HISTORY) - HISTORY_MAX_SESSIONS]:
                _HISTORY.pop(k, None)


def reset_history(session_key: str) -> None:
    with _HISTORY_LOCK:
        _HISTORY.pop(session_key, None)


# ---------------- 文本清洗 ----------------


_TAG_RE = re.compile(r"<\|?[a-zA-Z_]{2,}\|?>|</?think>|</?reasoning>", re.IGNORECASE)
_MD_RE = re.compile(r"(\*\*|__|`{1,3}|^#{1,6}\s|^\s*[-*]\s)", re.MULTILINE)
# 模型偶尔会"给建议"而不是"自己说话"，把这些前缀和包装剥掉
_ADVICE_RE = re.compile(
    r"^\s*(?:可以(?:这样|这么)?(?:回复|说|接)[:：]?|你可以(?:回复|说)[:：]?|"
    r"建议(?:回复|说)?[:：]?|回复[:：]|这样说[:：]?|接一句[:：]?)\s*",
    re.IGNORECASE)
_QUOTE_LINE_RE = re.compile(r"^\s*>\s?", re.MULTILINE)


def _clean(text: str) -> str:
    """去掉推理标签、Markdown 记号、建议式包装——QQ 上这些会变成乱码或不像人话。"""
    t = _TAG_RE.sub("", str(text))
    t = _MD_RE.sub("", t)
    t = _QUOTE_LINE_RE.sub("", t)
    t = _ADVICE_RE.sub("", t)
    # 整段被引号包住：去掉首尾引号
    t = t.strip()
    if len(t) >= 2 and t[0] in "「“\"'" and t[-1] in "」”\"'":
        t = t[1:-1].strip()
    # 模型爱给"选项 A / 选项 B"：只取第一行像话的
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    if len(lines) > 1:
        # 有多行时优先选第一个不带冒号收尾的短句
        for ln in lines:
            ln2 = _ADVICE_RE.sub("", ln).strip("“”「」\"' ")
            if ln2 and len(ln2) <= 60 and not ln2.endswith(("：", ":")):
                t = ln2
                break
        else:
            t = lines[0]
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    return t


def _post_one(p: dict[str, str], messages: list[dict[str, str]], system_prompt: str) -> str | None:
    """调用单个接口。推理型模型常把内容全写进 reasoning、正文留空，
    这类“空回复”会用更大的 max_tokens 重试一次。"""
    url = p["base"] + "/chat/completions"
    payload_messages: list[dict[str, str]] = []
    if system_prompt:
        payload_messages.append({"role": "system", "content": system_prompt})
    payload_messages.extend(messages)

    def attempt(max_tokens: int) -> tuple[str | None, str]:
        """返回 (正文, 失败原因)。"""
        payload: dict[str, Any] = {
            "model": p["model"],
            "messages": payload_messages,
            "temperature": 0.85,
            "max_tokens": max_tokens,
            "stream": False,
        }
        headers = {"Content-Type": "application/json"}
        if p.get("key"):
            headers["Authorization"] = "Bearer " + p["key"]

        try:
            with httpx.Client(timeout=TIMEOUT, trust_env=False) as client:
                resp = client.post(url, headers=headers, json=payload)
        except httpx.HTTPError as e:
            return None, f"网络失败: {e}"

        if resp.status_code >= 400:
            return None, f"HTTP {resp.status_code}: {resp.text[:160]}"

        try:
            data = resp.json()
        except ValueError:
            return None, "返回非 JSON"

        try:
            choices = data.get("choices") or []
            if not choices:
                return None, "无 choices"
            msg = choices[0].get("message", {}) or {}
            content = msg.get("content")
            if isinstance(content, list):  # 部分服务返回分段
                content = "".join(str(x.get("text", "")) for x in content if isinstance(x, dict))
            content = _clean(content) if content else ""
            if content:
                return content, ""

            # 正文为空：区分“只有推理”与“被 token 上限截断”
            reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
            finish = str(choices[0].get("finish_reason") or "")
            if finish == "length" or reasoning:
                return None, "EMPTY_REASONING"
            return None, "回复为空"
        except Exception as e:
            return None, f"解析失败: {e}"

    try:
        reply, reason = attempt(800)
        if reply:
            return reply
        if reason == "EMPTY_REASONING":
            # 推理吃掉了额度，放大上限再来一次
            reply, reason = attempt(2600)
            if reply:
                log.info("接口[%s] 空正文后重试成功", p["name"])
                return reply
        log.warning("接口[%s] %s", p["name"], reason)
        return None
    except Exception as e:
        log.warning("接口[%s] 异常: %s", p["name"], e)
        return None


def chat(user_text: str, system_prompt: str | None = None, session_key: str | None = None,
         speaker: str | None = None) -> str | None:
    """依次尝试所有接口，返回第一个成功的回复。全部失败返回 None。"""
    global _last_good

    c = _conf()
    if c["enabled"] != "1":
        return None
    if not user_text or not user_text.strip():
        return None

    plist = providers()
    if not plist:
        log.warning("AI 已启用但没有任何可用接口（请在设置里配置）")
        return None

    sys_prompt = system_prompt if system_prompt is not None else c["system_prompt"]
    messages = _history_get(session_key or "")
    content = user_text.strip()
    if speaker:
        content = f"{speaker}：{content}"
    messages.append({"role": "user", "content": content})

    errors: list[str] = []
    for p in plist:
        started = time.time()
        reply = _post_one(p, messages, sys_prompt)
        cost = time.time() - started
        if reply:
            _last_good = p["name"]
            _history_add(session_key or "", user_text.strip(), reply)
            log.info("AI 由接口[%s] 在 %.1fs 内回复成功（%d 字）", p["name"], cost, len(reply))
            return reply
        errors.append(p["name"])

    log.error("AI 所有接口均失败: %s", ", ".join(errors))
    return None


# 视觉模型默认值：看图专用（实测都能读 data: URL）
VISION_PROVIDERS: list[dict[str, str]] = [
    {"name": "VisionKimi", "base": "", "model": "trae/multimodal_image_kimi-k2.5", "key": ""},
    {"name": "CodeBuddyAuto", "base": "", "model": "codebuddy/auto", "key": ""},
    {"name": "GLM5V", "base": "", "model": "buddy/glm-5v-turbo", "key": ""},
]


def _vision_list() -> list[dict[str, str]]:
    """视觉模型列表。

    优先读 ai_vision_providers（独立配置，可指向另一个网关）；
    没配就沿用主接口里的地址和 Key，配默认视觉模型名。
    """
    c = _conf()
    explicit = _parse_providers((db.get_settings().get("ai_vision_providers") or "").strip())
    if explicit:
        return explicit

    base_items = _parse_providers(c["providers"])
    if not base_items:
        return []
    base0 = base_items[0]
    return [{
        "name": v["name"],
        "base": base0["base"],
        "model": v["model"],
        "key": base0["key"],
    } for v in VISION_PROVIDERS]


def vision_chat(prompt: str, image_data_url: str) -> str | None:
    """让视觉模型描述一张图。都失败返回 None。

    免费额度对同一模型的连续请求很敏感（429/502），所以：
    视觉模型轮流用、失败后短暂退避再换下一个，而不是一次就放弃。
    """
    c = _conf()
    if c["enabled"] != "1":
        return None
    items = _vision_list()
    if not items:
        return None

    # 全局限速：串行化看图调用，别并发打爆上游
    global _vision_last, _vision_cooldown_until
    with _vision_lock:
        now = time.time()
        slot = max(now, _vision_last + VISION_MIN_GAP)
        _vision_last = slot
    delay = slot - time.time()
    if delay > 0:
        time.sleep(min(delay, VISION_MIN_GAP))

    # 上次成功的视觉模型优先
    global _last_vision
    if _last_vision:
        items.sort(key=lambda p: 0 if p["name"] == _last_vision else 1)

    for attempt in range(2):          # 两轮，第一轮全试一遍，第二轮再试一次
        for p in items:
            payload = {
                "model": p["model"],
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": image_data_url}},
                    ],
                }],
                "max_tokens": 400,
                "temperature": 0.6,
                "stream": False,
            }
            headers = {"Content-Type": "application/json"}
            if p.get("key"):
                headers["Authorization"] = "Bearer " + p["key"]
            try:
                with httpx.Client(timeout=90.0, trust_env=False) as client:
                    resp = client.post(p["base"] + "/chat/completions", headers=headers, json=payload)
                if resp.status_code == 429:
                    log.warning("视觉[%s] 被限流，%.0fs 后重试", p["name"], 3 + attempt * 4)
                    time.sleep(3 + attempt * 4)
                    continue
                if resp.status_code >= 400:
                    log.warning("视觉[%s] HTTP %s: %s", p["name"], resp.status_code, resp.text[:120])
                    continue
                data = resp.json()
                msg = (data.get("choices") or [{}])[0].get("message", {}) or {}
                content = msg.get("content")
                if isinstance(content, list):
                    content = "".join(str(x.get("text", "")) for x in content if isinstance(x, dict))
                content = _clean(content) if content else ""
                if content:
                    _last_vision = p["name"]
                    log.info("视觉[%s] 看图成功（%d 字）", p["name"], len(content))
                    return content
            except httpx.HTTPError as e:
                log.warning("视觉[%s] 网络失败: %s", p["name"], e)
            except Exception as e:
                log.warning("视觉[%s] 异常: %s", p["name"], e)
        if attempt == 0:
            time.sleep(2)
    log.warning("这次没看成图（接口都忙），稍后再说")
    return None


def test() -> dict[str, Any]:
    global _last_good

    c = _conf()
    plist = providers()
    if not plist:
        return {"ok": False, "error": "没有配置任何接口", "results": []}

    results: list[dict[str, Any]] = []
    first_ok: dict[str, Any] | None = None

    for p in plist:
        started = time.time()
        reply = _post_one(p, [{"role": "user", "content": TEST_PROMPT}], c["system_prompt"])
        cost = round(time.time() - started, 1)
        item = {
            "name": p["name"],
            "base": p["base"],
            "model": p["model"],
            "ok": bool(reply),
            "seconds": cost,
            "reply": (reply or "")[:120],
        }
        results.append(item)
        if reply and first_ok is None:
            first_ok = item
            _last_good = p["name"]

    return {
        "ok": bool(first_ok),
        "winner": first_ok["name"] if first_ok else None,
        "results": results,
    }

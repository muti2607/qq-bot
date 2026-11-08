"""AI 聊天：多接口自动故障转移。

支持任意兼容 OpenAI /v1/chat/completions 的服务，并在多个接口间自动切换：
配置了多个接口时，按顺序尝试；某个接口报错/超时/返回空，立刻换下一个。

配置方式（后台“设置 → AI 聊天”）：
  ai_providers 每行一个接口，格式：
      名称|接口地址|模型名|API Key
  例如：
      智谱|https://open.bigmodel.cn/api/paas/v4|glm-4-flash|xxxxxxxx
      硅基流动|https://api.siliconflow.cn/v1|Qwen/Qwen2.5-7B-Instruct|sk-xxxxxxxx
      DeepSeek|https://api.deepseek.com/v1|deepseek-chat|sk-xxxxxxxx

  若 ai_providers 留空，则回退使用 ai_api_base / ai_model / ai_api_key 的单接口配置。

  额外内置一个**无需 Key 的免费兜底接口**（Pollinations），
  由 ai_keyless_fallback 控制是否启用，默认开启。
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

import db
import httpx

log = logging.getLogger("qqbot.ai")

TIMEOUT = 25.0
TEST_PROMPT = "用一句简短的中文跟我打个招呼"

# 记住上次成功的接口名，下次优先尝试它
_last_good: str | None = None

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


def _post_one(p: dict[str, str], user_text: str, system_prompt: str) -> str | None:
    url = p["base"] + "/chat/completions"
    messages: list[dict[str, str]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": user_text})

    payload: dict[str, Any] = {
        "model": p["model"],
        "messages": messages,
        "temperature": 0.7,
        "stream": False,
    }
    headers = {"Content-Type": "application/json"}
    if p.get("key"):
        headers["Authorization"] = "Bearer " + p["key"]

    try:
        with httpx.Client(timeout=TIMEOUT, trust_env=False) as client:
            resp = client.post(url, headers=headers, json=payload)
    except httpx.HTTPError as e:
        log.warning("接口[%s] 网络失败: %s", p["name"], e)
        return None

    if resp.status_code >= 400:
        log.warning("接口[%s] HTTP %s: %s", p["name"], resp.status_code, resp.text[:200])
        return None

    try:
        data = resp.json()
    except ValueError:
        log.warning("接口[%s] 返回非 JSON", p["name"])
        return None

    try:
        choices = data.get("choices") or []
        if not choices:
            log.warning("接口[%s] 无 choices", p["name"])
            return None
        content = choices[0].get("message", {}).get("content")
        if isinstance(content, list):  # 部分服务返回分段
            content = "".join(str(x.get("text", "")) for x in content if isinstance(x, dict))
        if not content or not str(content).strip():
            log.warning("接口[%s] 回复为空", p["name"])
            return None
        return str(content).strip()
    except Exception as e:
        log.warning("接口[%s] 解析失败: %s", p["name"], e)
        return None


def chat(user_text: str, system_prompt: str | None = None) -> str | None:
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
    errors: list[str] = []

    for p in plist:
        started = time.time()
        reply = _post_one(p, user_text.strip(), sys_prompt)
        cost = time.time() - started
        if reply:
            _last_good = p["name"]
            log.info("AI 由接口[%s] 在 %.1fs 内回复成功", p["name"], cost)
            return reply
        errors.append(p["name"])

    log.error("AI 所有接口均失败: %s", ", ".join(errors))
    return None


def test() -> dict[str, Any]:
    """逐个测试接口，返回每个接口的结果（给后台“测试”按钮用）。"""
    global _last_good

    c = _conf()
    plist = providers()
    if not plist:
        return {"ok": False, "error": "没有配置任何接口", "results": []}

    results: list[dict[str, Any]] = []
    first_ok: dict[str, Any] | None = None

    for p in plist:
        started = time.time()
        reply = _post_one(p, TEST_PROMPT, c["system_prompt"])
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

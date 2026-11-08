"""AI 聊天：调用兼容 OpenAI /v1/chat/completions 的大模型接口。

适用于任意提供 OpenAI 格式接口的免费/付费模型，例如：
- DeepSeek：https://api.deepseek.com/v1
- SiliconFlow：https://api.siliconflow.cn/v1
- OpenRouter：https://openrouter.ai/api/v1
- Ollama（本地）：http://127.0.0.1:11434/v1

配置项在机器人后台“设置”→“AI 聊天”里填写：
  AI 接口地址（api_base）、API Key、模型名、系统提示词、回复模式。
"""
from __future__ import annotations

import logging
from typing import Any

import db
import httpx

log = logging.getLogger("qqbot.ai")

TIMEOUT = 30.0


def _conf() -> dict[str, str]:
    s = db.get_settings()
    return {
        "enabled": str(s.get("ai_enabled", "0")),
        "api_base": (s.get("ai_api_base") or "").rstrip("/"),
        "api_key": s.get("ai_api_key") or "",
        "model": s.get("ai_model") or "",
        "system_prompt": s.get("ai_system_prompt") or "",
        "reply_mode": str(s.get("ai_reply_mode") or "off"),
    }


def is_enabled() -> bool:
    return _conf()["enabled"] == "1"


def reply_mode() -> str:
    return _conf()["reply_mode"]


def chat(user_text: str, system_prompt: str | None = None) -> str | None:
    """调用大模型获取回复文本。失败或未配置时返回 None。"""
    c = _conf()
    if c["enabled"] != "1":
        return None
    if not c["api_base"] or not c["api_key"] or not c["model"]:
        log.warning("AI 已启用但未配置 api_base / api_key / model，无法调用")
        return None
    if not user_text or not user_text.strip():
        return None

    url = c["api_base"] + "/chat/completions"
    messages: list[dict[str, str]] = []
    sys_prompt = system_prompt or c["system_prompt"]
    if sys_prompt:
        messages.append({"role": "system", "content": sys_prompt})
    messages.append({"role": "user", "content": user_text.strip()})

    payload: dict[str, Any] = {
        "model": c["model"],
        "messages": messages,
        "temperature": 0.7,
        "stream": False,
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {c['api_key']}",
    }
    try:
        with httpx.Client(timeout=TIMEOUT, trust_env=False) as client:
            resp = client.post(url, headers=headers, json=payload)
    except httpx.HTTPError as e:
        log.error("AI 请求网络失败：%s", e)
        return None
    if resp.status_code >= 400:
        log.error("AI API HTTP %s: %s", resp.status_code, resp.text[:300])
        return None
    try:
        data = resp.json()
    except ValueError as e:
        log.error("AI 返回非 JSON：%s", e)
        return None

    try:
        choices = data.get("choices") or []
        if not choices:
            return None
        content = choices[0].get("message", {}).get("content")
        return content.strip() if content else None
    except Exception as e:
        log.error("AI 返回解析失败：%s", e)
        return None

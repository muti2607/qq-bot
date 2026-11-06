"""内容渲染：纯文本模板、图片、RSS 抓取。"""
from __future__ import annotations

import datetime as _dt
import re
from pathlib import Path
from zoneinfo import ZoneInfo

import db
import onebot

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"[ \t\u3000]+")


def strip_html(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p>", "\n", text)
    text = _TAG_RE.sub("", text)
    import html as _html

    text = _html.unescape(text)
    text = _WS_RE.sub(" ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def render_template(content: str, task: dict, now: _dt.datetime) -> str:
    """支持占位符：{date} {time} {datetime} {weekday} {name}"""
    weekday_cn = "一二三四五六日"[now.weekday()]
    mapping = {
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M"),
        "datetime": now.strftime("%Y-%m-%d %H:%M"),
        "weekday": f"星期{weekday_cn}",
        "name": task.get("name") or "",
    }

    def repl(m: re.Match) -> str:
        key = m.group(1)
        return mapping.get(key, m.group(0))

    return re.sub(r"\{(\w+)\}", repl, content or "")


def fetch_rss(url: str, timeout: int = 15) -> dict:
    """抓取 RSS/Atom 最新一条，返回 {title, link, summary, published}。"""
    import feedparser

    parsed = feedparser.parse(url, agent="Mozilla/5.0 (compatible; QQDailyBot/1.0)")
    if getattr(parsed, "bozo", 0) and not parsed.entries:
        raise RuntimeError(f"RSS 解析失败：{getattr(parsed, 'bozo_exception', '未知错误')}")
    if not parsed.entries:
        raise RuntimeError("RSS 中没有条目")
    e = parsed.entries[0]
    summary = ""
    if e.get("summary"):
        summary = strip_html(e["summary"])
    elif e.get("content"):
        summary = strip_html(e["content"][0].get("value", ""))
    published = ""
    if e.get("published") or e.get("updated"):
        published = e.get("published") or e.get("updated")
    return {
        "title": strip_html(e.get("title", "")),
        "link": e.get("link", ""),
        "summary": summary,
        "published": published,
    }


def build_segments(task: dict, now: _dt.datetime) -> list[dict]:
    """根据任务配置生成消息段列表。抛异常表示本条无法发送。"""
    mode = task.get("source_mode") or "static"
    text = render_template(task.get("content") or "", task, now)
    timeout = 15
    try:
        timeout = int(db.get_settings().get("rss_timeout") or 15)
    except ValueError:
        pass

    if mode in ("rss_text", "rss_with_link"):
        url = render_template(task.get("rss_url") or "", task, now).strip()
        if not url:
            raise RuntimeError("RSS 模式但未填写 RSS 地址")
        item = fetch_rss(url, timeout)
        title = item["title"] or "新内容"
        parts = []
        if text:
            parts.append(text)
        parts.append(f"【{title}】")
        if item["summary"]:
            parts.append(item["summary"][:500])
        if mode == "rss_with_link" and item["link"]:
            parts.append(item["link"])
        text = "\n".join(parts)

    segments: list[dict] = []
    if text.strip():
        segments.append(onebot.text_segment(text.strip()))

    image_id = task.get("image_id")
    if image_id:
        img = db.get_image(int(image_id))
        if not img:
            raise RuntimeError(f"图片 id={image_id} 不存在")
        path = db.UPLOAD_DIR / img["filename"]
        if not path.exists():
            raise RuntimeError(f"图片文件丢失：{img['filename']}")
        segments.append(onebot.image_segment(path))

    if not segments:
        raise RuntimeError("内容为空（既没有文本也没有图片）")
    return segments


def tz_of(task: dict) -> ZoneInfo:
    try:
        return ZoneInfo(task.get("timezone") or "Asia/Shanghai")
    except Exception:
        return ZoneInfo("Asia/Shanghai")

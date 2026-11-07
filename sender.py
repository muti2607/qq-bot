"""内容渲染：纯文本模板、图片、RSS 抓取。"""
from __future__ import annotations

import ast
import datetime as _dt
import random
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


_COUNTDOWN_RE = re.compile(
    r"\{(days|date|label|weekday_of):([0-9]{3,4}|[0-9]{1,2}[-/][0-9]{1,2}|[0-9]{4}-[0-9]{2}-[0-9]{2})\}"
)


def _parse_target(raw: str, now: _dt.datetime) -> _dt.date:
    """把 1209 / 12-09 / 12/9 / 2027-03-01 解析成具体日期。
    只给月日时：若今年已过，自动顺延到明年。"""
    s = raw.replace("/", "-")
    if re.fullmatch(r"\d{3,4}", s):  # MMDD 或 MDD，如 1209 / 129
        if len(s) == 4:
            month, day = int(s[:2]), int(s[2:])
        else:
            month, day = int(s[0]), int(s[1:])
    elif re.fullmatch(r"\d{1,2}-\d{1,2}", s):
        month, day = (int(x) for x in s.split("-"))
    else:
        try:
            y, m, d = (int(x) for x in s.split("-"))
            return _dt.date(y, m, d)
        except ValueError as e:
            raise ValueError(f"无法识别的日期 {raw}（应写成 1209、12-09 或 2027-03-01）：{e}") from e
    try:
        target = _dt.date(now.year, month, day)
    except ValueError as e:
        raise ValueError(f"非法日期 {raw}：{e}") from e
    if target < now.date():
        try:
            target = _dt.date(now.year + 1, month, day)
        except ValueError as e:
            raise ValueError(f"非法日期 {raw}：{e}") from e
    return target


_EXPR_RE = re.compile(r"\{=([^{}]+)\}")


def _safe_eval(expr: str) -> float:
    """只允许数字与 + - * / // % ** ( ) 的表达式，用 ast 白名单校验，绝不执行任意代码。"""
    expr = expr.strip()
    if not expr or len(expr) > 100:
        raise ValueError("表达式为空或过长")
    if not re.fullmatch(r"[\d\.\s\+\-\*/\%()]+", expr):
        raise ValueError("只支持数字与 + - * / // % ** ( ) 运算")
    tree = ast.parse(expr, mode="eval")

    import operator
    _OPS = {
        ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
        ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv,
        ast.Mod: operator.mod, ast.Pow: operator.pow,
    }

    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
            a, b = ev(node.left), ev(node.right)
            if isinstance(node.op, (ast.Div, ast.Mod, ast.FloorDiv)) and b == 0:
                raise ValueError("除数为 0")
            if isinstance(node.op, ast.Pow) and (abs(b) > 64 or abs(a) > 10 ** 9):
                raise ValueError("幂运算超出限制")
            return _OPS[type(node.op)](a, b)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            v = ev(node.operand)
            return +v if isinstance(node.op, ast.UAdd) else -v
        raise ValueError("表达式包含不支持的写法")

    result = ev(tree)
    if isinstance(result, float) and result.is_integer():
        return int(result)
    if isinstance(result, float):
        return round(result, 4)
    return result


def _fmt_num(x) -> str:
    return str(x)


def random_quote() -> str:
    """从名言池随机取一条，格式：「名言」 ——出处"""
    q = random.choice(db.get_quotes())
    if "|" in q:
        text, who = q.split("|", 1)
        return f"“{text.strip()}” ——{who.strip()}"
    return f"“{q.strip()}”"


def render_template(content: str, task: dict, now: _dt.datetime) -> str:
    """支持占位符：
    {date} {time} {datetime} {weekday} {name} {quote}
    自定义变量：任务 vars 里的 {key}
    倒计时：{days:1209} {days:2027-03-01} {date:1209} {label:1209} {weekday_of:1209}
    算术：{=115-30} {=(2027-2026)*365}
    """
    weekday_cn = "一二三四五六日"[now.weekday()]
    mapping = {
        "date": now.strftime("%Y-%m-%d"),
        "time": now.strftime("%H:%M"),
        "datetime": now.strftime("%Y-%m-%d %H:%M"),
        "weekday": f"星期{weekday_cn}",
        "name": task.get("name") or "",
    }
    for var in task.get("vars") or []:
        key = (var.get("key") or "").strip()
        if key and key not in mapping:
            mapping[key] = var.get("value") or ""

    def repl(m: re.Match) -> str:
        key = m.group(1)
        if key == "quote":
            return random_quote()
        return mapping.get(key, m.group(0))

    text = re.sub(r"\{(\w+)\}", repl, content or "")

    def cd_repl(m: re.Match) -> str:
        kind, raw = m.group(1), m.group(2)
        try:
            target = _parse_target(raw, now)
        except ValueError as e:
            return f"[{e}]"
        if kind == "days":
            return str((target - now.date()).days)
        if kind == "date":
            return target.strftime("%Y-%m-%d")
        if kind == "label":
            return f"{target.month}月{target.day}日"
        return f"星期{'一二三四五六日'[target.weekday()]}"

    text = _COUNTDOWN_RE.sub(cd_repl, text)

    def expr_repl(m: re.Match) -> str:
        try:
            return _fmt_num(_safe_eval(m.group(1)))
        except Exception as e:
            return f"[算式错误:{e}]"

    return _EXPR_RE.sub(expr_repl, text)


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


def pick_image(task_or_trigger: dict) -> Path | None:
    """按 image_mode 选图：fixed 用单张；random 从候选池（空=全库）随机抽一张。"""
    mode = task_or_trigger.get("image_mode") or "fixed"
    if mode == "none":
        return None
    if mode == "random":
        pool = task_or_trigger.get("image_ids") or []
        candidates = []
        for iid in pool:
            img = db.get_image(int(iid))
            if img and (db.UPLOAD_DIR / img["filename"]).exists():
                candidates.append(db.UPLOAD_DIR / img["filename"])
        if not candidates:
            all_imgs = [
                db.UPLOAD_DIR / i["filename"]
                for i in db.list_images()
                if (db.UPLOAD_DIR / i["filename"]).exists()
            ]
            candidates = all_imgs
        if not candidates:
            raise RuntimeError("随机图片：图片库为空，请先上传图片")
        return random.choice(candidates)
    image_id = task_or_trigger.get("image_id")
    if not image_id:
        return None
    img = db.get_image(int(image_id))
    if not img:
        raise RuntimeError(f"图片 id={image_id} 不存在")
    path = db.UPLOAD_DIR / img["filename"]
    if not path.exists():
        raise RuntimeError(f"图片文件丢失：{img['filename']}")
    return path


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

    try:
        img_path = pick_image(task)
    except RuntimeError:
        if not (task.get("content") or "").strip() and (task.get("source_mode") or "static") == "static":
            raise
        img_path = None  # 有文本时图片选不出来就只发文本，不让整条任务失败
    # 图片放消息最前：QQ 对"先长文后图"会折叠成卡片，先图后文则展示原图
    if img_path:
        segments.append(onebot.image_segment(img_path))
    if text.strip():
        segments.append(onebot.text_segment(text.strip()))

    if not segments:
        raise RuntimeError("内容为空（既没有文本也没有图片）")
    return segments


def tz_of(task: dict) -> ZoneInfo:
    try:
        return ZoneInfo(task.get("timezone") or "Asia/Shanghai")
    except Exception:
        return ZoneInfo("Asia/Shanghai")

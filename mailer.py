"""QQ 邮箱 SMTP 发信：发送注册验证码。"""
from __future__ import annotations

import random
import smtplib
from email.mime.text import MIMEText
from email.header import Header
from email.utils import formataddr

import db

# 验证码存储：邮箱 -> (code, expires_ts, attempts)
import time

_codes: dict[str, tuple[str, float, int]] = {}
_CODE_TTL = 600          # 10 分钟有效
_MAX_ATTEMPTS = 5        # 最多校验 5 次
_SEND_INTERVAL = 60      # 同一邮箱 60 秒只能发一次
_last_send: dict[str, float] = {}


class MailError(RuntimeError):
    pass


def _smtp_conf() -> dict:
    s = db.get_settings()
    host = s.get("smtp_host") or "smtp.qq.com"
    port = int(s.get("smtp_port") or 465)
    user = s.get("smtp_user") or ""
    pwd = s.get("smtp_pass") or ""
    if not user or not pwd:
        raise MailError("管理员尚未配置邮箱 SMTP，无法发送验证码")
    return {"host": host, "port": port, "user": user, "pwd": pwd}


def _valid_qq_mail(addr: str) -> bool:
    import re
    return bool(re.fullmatch(r"[1-9]\d{4,10}@qq\.com", addr.strip().lower()))


def send_code(mail: str) -> int:
    """生成并发送验证码，返回有效期秒数。"""
    mail = mail.strip().lower()
    if not _valid_qq_mail(mail):
        raise MailError("仅支持 QQ 邮箱注册")
    now = time.time()
    last = _last_send.get(mail, 0)
    if now - last < _SEND_INTERVAL:
        raise MailError(f"发送太频繁，请 {int(_SEND_INTERVAL - (now - last)) + 1} 秒后再试")
    code = f"{random.randint(0, 999999):06d}"
    conf = _smtp_conf()

    body = (
        f"<div style='font-family:sans-serif;max-width:480px;margin:0 auto;padding:24px;'>"
        f"<h2 style='color:#ec4899;'>QQ 机器人 · 图片上传注册</h2>"
        f"<p>你的验证码：</p>"
        f"<p style='font-size:32px;font-weight:800;letter-spacing:6px;color:#111;"
        f"background:#f6f7fb;padding:14px 20px;border-radius:10px;text-align:center;'>{code}</p>"
        f"<p style='color:#888;font-size:13px;'>10 分钟内有效。如果不是你本人操作，请忽略本邮件。</p>"
        f"</div>"
    )
    msg = MIMEText(body, "html", "utf-8")
    msg["Subject"] = Header(f"注册验证码 {code}", "utf-8")
    msg["From"] = formataddr(("QQ 机器人", conf["user"]))
    msg["To"] = formataddr(("申请者", mail))

    try:
        if conf["port"] == 465:
            smtp = smtplib.SMTP_SSL(conf["host"], conf["port"], timeout=15)
        else:
            smtp = smtplib.SMTP(conf["host"], conf["port"], timeout=15)
            smtp.starttls()
        smtp.login(conf["user"], conf["pwd"])
        smtp.sendmail(conf["user"], [mail], msg.as_string())
        smtp.quit()
    except Exception as e:
        raise MailError(f"邮件发送失败：{e}") from e

    _codes[mail] = (code, now + _CODE_TTL, 0)
    _last_send[mail] = now
    return _CODE_TTL


def verify_code(mail: str, code: str) -> bool:
    mail = mail.strip().lower()
    rec = _codes.get(mail)
    if not rec:
        return False
    saved, expires, attempts = rec
    if time.time() > expires:
        _codes.pop(mail, None)
        return False
    if attempts >= _MAX_ATTEMPTS:
        _codes.pop(mail, None)
        return False
    if saved == code.strip():
        _codes.pop(mail, None)
        return True
    _codes[mail] = (saved, expires, attempts + 1)
    return False

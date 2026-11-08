# -*- coding: utf-8 -*-
"""服务器侧：带真实登录态获取 / 页面 HTML 并检查移动端 CSS 是否命中。
密码从 bot.db 的 admin_password 读取（若为空则 / 不重定向）。"""
import sys
sys.stdout.reconfigure(encoding="utf-8")
import sqlite3, json, urllib.request, http.cookiejar

conn = sqlite3.connect("/opt/qq-bot/data/bot.db")
rows = dict(conn.execute("SELECT key, value FROM settings").fetchall())
pw = rows.get("admin_password", "")

cj = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
base = "http://127.0.0.1:8080"
try:
    if pw:
        req = urllib.request.Request(base + "/api/login",
                                     data=json.dumps({"password": pw}).encode(),
                                     headers={"Content-Type": "application/json"})
        opener.open(req, timeout=10).read()
    req = urllib.request.Request(base + "/", headers={"User-Agent": "iPhone Mobile Safari"})
    h = opener.open(req, timeout=10).read().decode("utf-8", "replace")
    print("html bytes:", len(h))
    print("overflow-x:auto:", h.count("overflow-x:auto"))
    print("single media 980:", h.count("@media (max-width:980px)"))
    print("hidden def:", ".hidden{display:none !important}" in h)
    print("lightbox hidden:", 'class="lightbox hidden"' in h)
    print("nav count:", h.count('data-nav="'))
    print("viewport:", "width=device-width" in h)
except Exception as e:
    print("ERR:", type(e).__name__, e)
